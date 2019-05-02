# Copyright (C) 2018 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

from typing import NamedTuple, Iterable, TYPE_CHECKING
import os
import queue
import threading
import concurrent
from collections import defaultdict
import asyncio
from enum import IntEnum, auto
from typing import NamedTuple, Dict
import jsonrpclib

from sqlalchemy import Column, ForeignKey, Integer, String, DateTime, Boolean
from sqlalchemy.orm.query import Query
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import not_, or_
from .sql_db import SqlDB, sql

from .util import bh2u, bfh, log_exceptions, ignore_exceptions
from . import wallet
from .storage import WalletStorage
from .address_synchronizer import AddressSynchronizer, TX_HEIGHT_LOCAL, TX_HEIGHT_UNCONF_PARENT, TX_HEIGHT_UNCONFIRMED
from .transaction import Transaction

if TYPE_CHECKING:
    from .network import Network

class ListenerItem(NamedTuple):
    # this is triggered when the lnwatcher is all done with the outpoint used as index in LNWatcher.tx_progress
    all_done : asyncio.Event
    # txs we broadcast are put on this queue so that the test can wait for them to get mined
    tx_queue : asyncio.Queue

class TxMinedDepth(IntEnum):
    """ IntEnum because we call min() in get_deepest_tx_mined_depth_for_txids """
    DEEP = auto()
    SHALLOW = auto()
    MEMPOOL = auto()
    FREE = auto()


Base = declarative_base()

class SweepTx(Base):
    __tablename__ = 'sweep_txs'
    funding_outpoint = Column(String(34), primary_key=True)
    index = Column(Integer(), primary_key=True)
    prev_txid = Column(String(32))
    tx = Column(String())

class ChannelInfo(Base):
    __tablename__ = 'channel_info'
    outpoint = Column(String(34), primary_key=True)
    address = Column(String(32))



class SweepStore(SqlDB):

    def __init__(self, path, network):
        super().__init__(network, path, Base)

    @sql
    def get_sweep_tx(self, funding_outpoint, prev_txid):
        return [Transaction(bh2u(r.tx)) for r in self.DBSession.query(SweepTx).filter(SweepTx.funding_outpoint==funding_outpoint, SweepTx.prev_txid==prev_txid).all()]

    @sql
    def get_tx_by_index(self, funding_outpoint, index):
        r = self.DBSession.query(SweepTx).filter(SweepTx.funding_outpoint==funding_outpoint, SweepTx.index==index).one_or_none()
        return r.prev_txid, bh2u(r.tx)

    @sql
    def list_sweep_tx(self):
        return set(r.funding_outpoint for r in self.DBSession.query(SweepTx).all())

    @sql
    def add_sweep_tx(self, funding_outpoint, prev_txid, tx):
        n = self.DBSession.query(SweepTx).filter(funding_outpoint==funding_outpoint).count()
        self.DBSession.add(SweepTx(funding_outpoint=funding_outpoint, index=n, prev_txid=prev_txid, tx=bfh(tx)))
        self.DBSession.commit()

    @sql
    def get_num_tx(self, funding_outpoint):
        return self.DBSession.query(SweepTx).filter(funding_outpoint==funding_outpoint).count()

    @sql
    def remove_sweep_tx(self, funding_outpoint):
        r = self.DBSession.query(SweepTx).filter(SweepTx.funding_outpoint==funding_outpoint).all()
        for x in r:
            self.DBSession.delete(x)
        self.DBSession.commit()

    @sql
    def add_channel(self, outpoint, address):
        self.DBSession.add(ChannelInfo(address=address, outpoint=outpoint))
        self.DBSession.commit()

    @sql
    def remove_channel(self, outpoint):
        v = self.DBSession.query(ChannelInfo).filter(ChannelInfo.outpoint==outpoint).one_or_none()
        self.DBSession.delete(v)
        self.DBSession.commit()

    @sql
    def has_channel(self, outpoint):
        return bool(self.DBSession.query(ChannelInfo).filter(ChannelInfo.outpoint==outpoint).one_or_none())

    @sql
    def get_address(self, outpoint):
        r = self.DBSession.query(ChannelInfo).filter(ChannelInfo.outpoint==outpoint).one_or_none()
        return r.address if r else None

    @sql
    def list_channel_info(self):
        return [(r.address, r.outpoint) for r in self.DBSession.query(ChannelInfo).all()]


class LNWatcher(AddressSynchronizer):
    verbosity_filter = 'W'

    def __init__(self, network: 'Network'):
        path = os.path.join(network.config.path, "watchtower_wallet")
        storage = WalletStorage(path)
        AddressSynchronizer.__init__(self, storage)
        self.config = network.config
        self.start_network(network)
        self.lock = threading.RLock()
        self.sweepstore = SweepStore(os.path.join(network.config.path, "watchtower_db"), network)
        self.network.register_callback(self.on_network_update,
                                       ['network_updated', 'blockchain_updated', 'verified', 'wallet_updated'])
        self.set_remote_watchtower()
        # this maps funding_outpoints to ListenerItems, which have an event for when the watcher is done,
        # and a queue for seeing which txs are being published
        self.tx_progress = {} # type: Dict[str, ListenerItem]
        # status gets populated when we run
        self.channel_status = {}

    def get_channel_status(self, outpoint):
        return self.channel_status.get(outpoint, 'unknown')

    def set_remote_watchtower(self):
        watchtower_url = self.config.get('watchtower_url')
        self.watchtower = jsonrpclib.Server(watchtower_url) if watchtower_url else None
        self.watchtower_queue = asyncio.Queue()

    def get_num_tx(self, outpoint):
        return self.sweepstore.get_num_tx(outpoint)

    @ignore_exceptions
    @log_exceptions
    async def watchtower_task(self):
        self.logger.info('watchtower task started')
        # initial check
        for address, outpoint in self.sweepstore.list_channel_info():
            await self.watchtower_queue.put(outpoint)
        while True:
            outpoint = await self.watchtower_queue.get()
            if self.watchtower is None:
                continue
            # synchronize with remote
            try:
                local_n = self.sweepstore.get_num_tx(outpoint)
                n = self.watchtower.get_num_tx(outpoint)
                if n == 0:
                    address = self.sweepstore.get_address(outpoint)
                    self.watchtower.add_channel(outpoint, address)
                self.logger.info("sending %d transactions to watchtower"%(local_n - n))
                for index in range(n, local_n):
                    prev_txid, tx = self.sweepstore.get_tx_by_index(outpoint, index)
                    self.watchtower.add_sweep_tx(outpoint, prev_txid, tx)
            except ConnectionRefusedError:
                self.logger.info('could not reach watchtower, will retry in 5s')
                await asyncio.sleep(5)
                await self.watchtower_queue.put(outpoint)

    def add_channel(self, outpoint, address):
        self.add_address(address)
        with self.lock:
            if not self.sweepstore.has_channel(outpoint):
                self.sweepstore.add_channel(outpoint, address)

    def unwatch_channel(self, address, funding_outpoint):
        self.logger.info(f'unwatching {funding_outpoint}')
        self.sweepstore.remove_sweep_tx(funding_outpoint)
        self.sweepstore.remove_channel(funding_outpoint)
        if funding_outpoint in self.tx_progress:
            self.tx_progress[funding_outpoint].all_done.set()

    @log_exceptions
    async def on_network_update(self, event, *args):
        if event in ('verified', 'wallet_updated'):
            if args[0] != self:
                return
        if not self.synchronizer:
            self.logger.info("synchronizer not set yet")
            return
        if not self.synchronizer.is_up_to_date():
            return
        for address, outpoint in self.sweepstore.list_channel_info():
            await self.check_onchain_situation(address, outpoint)

    async def check_onchain_situation(self, address, funding_outpoint):
        keep_watching, spenders = self.inspect_tx_candidate(funding_outpoint, 0)
        funding_txid = funding_outpoint.split(':')[0]
        funding_height = self.get_tx_height(funding_txid)
        closing_txid = spenders.get(funding_outpoint)
        if closing_txid is None:
            self.network.trigger_callback('channel_open', funding_outpoint, funding_txid, funding_height)
        else:
            closing_height = self.get_tx_height(closing_txid)
            self.network.trigger_callback('channel_closed', funding_outpoint, spenders, funding_txid, funding_height, closing_txid, closing_height)
            await self.do_breach_remedy(funding_outpoint, spenders)
        if not keep_watching:
            self.unwatch_channel(address, funding_outpoint)
        else:
            #self.logger.info(f'we will keep_watching {funding_outpoint}')
            pass

    def inspect_tx_candidate(self, outpoint, n):
        # FIXME: instead of stopping recursion at n == 2,
        # we should detect which outputs are HTLCs
        prev_txid, index = outpoint.split(':')
        txid = self.db.get_spent_outpoint(prev_txid, int(index))
        result = {outpoint:txid}
        if txid is None:
            self.channel_status[outpoint] = 'open'
            #self.logger.info('keep watching because outpoint is unspent')
            return True, result
        keep_watching = (self.get_tx_mined_depth(txid) != TxMinedDepth.DEEP)
        if keep_watching:
            self.channel_status[outpoint] = 'closed (%d)' % self.get_tx_height(txid).conf
            #self.logger.info('keep watching because spending tx is not deep')
        else:
            self.channel_status[outpoint] = 'closed (deep)'

        tx = self.db.get_transaction(txid)
        for i, o in enumerate(tx.outputs()):
            if o.address not in self.get_addresses():
                self.add_address(o.address)
                keep_watching = True
            elif n < 2:
                k, r = self.inspect_tx_candidate(txid+':%d'%i, n+1)
                keep_watching |= k
                result.update(r)
        return keep_watching, result

    async def do_breach_remedy(self, funding_outpoint, spenders):
        for prevout, spender in spenders.items():
            if spender is not None:
                continue
            prev_txid, prev_n = prevout.split(':')
            sweep_txns = self.sweepstore.get_sweep_tx(funding_outpoint, prev_txid)
            for tx in sweep_txns:
                if not await self.broadcast_or_log(funding_outpoint, tx):
                    self.logger.info(f'{tx.name} could not publish tx: {str(tx)}, prev_txid: {prev_txid}')

    async def broadcast_or_log(self, funding_outpoint, tx):
        height = self.get_tx_height(tx.txid()).height
        if height != TX_HEIGHT_LOCAL:
            return
        try:
            txid = await self.network.broadcast_transaction(tx)
        except Exception as e:
            self.logger.info(f'broadcast: {tx.name}: failure: {repr(e)}')
        else:
            self.logger.info(f'broadcast: {tx.name}: success. txid: {txid}')
            if funding_outpoint in self.tx_progress:
                await self.tx_progress[funding_outpoint].tx_queue.put(tx)
            return txid

    def add_sweep_tx(self, funding_outpoint: str, prev_txid: str, tx: str):
        self.sweepstore.add_sweep_tx(funding_outpoint, prev_txid, tx)
        if self.watchtower:
            self.watchtower_queue.put_nowait(funding_outpoint)

    def get_tx_mined_depth(self, txid: str):
        if not txid:
            return TxMinedDepth.FREE
        tx_mined_depth = self.get_tx_height(txid)
        height, conf = tx_mined_depth.height, tx_mined_depth.conf
        if conf > 100:
            return TxMinedDepth.DEEP
        elif conf > 0:
            return TxMinedDepth.SHALLOW
        elif height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT):
            return TxMinedDepth.MEMPOOL
        elif height == TX_HEIGHT_LOCAL:
            return TxMinedDepth.FREE
        elif height > 0 and conf == 0:
            # unverified but claimed to be mined
            return TxMinedDepth.MEMPOOL
        else:
            raise NotImplementedError()
