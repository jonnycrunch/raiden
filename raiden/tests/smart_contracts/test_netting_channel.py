# -*- coding: utf-8 -*-
# pylint: disable=no-member
from __future__ import division

import pytest
from ethereum import abi, tester, slogging
from ethereum.tester import TransactionFailed
from ethereum.utils import encode_hex
from secp256k1 import PrivateKey

from raiden.encoding.signing import GLOBAL_CTX
from raiden.utils import sha3, privatekey_to_address
from raiden.tests.utils.tester import (
    new_nettingcontract,
)
from raiden.messages import Lock
from raiden.transfer.state_change import Block

# TODO:
# - change the hashroot and check older locks are not freed
# - add locked amounts and assert that they are respected


def increase_transferred_amount(from_channel, to_channel, amount):
    from_channel.our_state.transferred_amount += amount
    to_channel.partner_state.transferred_amount += amount


def make_direct_transfer(channel, partner_channel, amount, pkey):
    identifier = channel.our_state.nonce

    direct_transfer = channel.create_directtransfer(
        amount,
        identifier=identifier,
    )

    address = privatekey_to_address(pkey)
    sign_key = PrivateKey(pkey, ctx=GLOBAL_CTX, raw=True)
    direct_transfer.sign(sign_key, address)

    # if this fails it's not the right key for the current `channel`
    assert direct_transfer.sender == channel.our_state.address

    channel.register_transfer(direct_transfer)
    partner_channel.register_transfer(direct_transfer)

    return direct_transfer


def make_mediated_transfer(
        channel,
        partner_channel,
        initiator,
        target,
        lock,
        pkey,
        block_number,
        secret=None):
    identifier = channel.our_state.nonce
    fee = 0

    receiver = channel.partner_state.address

    mediated_transfer = channel.create_mediatedtransfer(
        initiator,
        receiver,
        fee,
        lock.amount,
        identifier,
        lock.expiration,
        lock.hashlock,
    )

    address = privatekey_to_address(pkey)
    sign_key = PrivateKey(pkey, ctx=GLOBAL_CTX, raw=True)
    mediated_transfer.sign(sign_key, address)

    channel.block_number = block_number
    partner_channel.block_number = block_number

    # if this fails it's not the right key for the current `channel`
    assert mediated_transfer.sender == channel.our_state.address

    channel.register_transfer(mediated_transfer)
    partner_channel.register_transfer(mediated_transfer)

    if secret is not None:
        channel.register_secret(secret)
        partner_channel.register_secret(secret)

    return mediated_transfer


@pytest.mark.parametrize('number_of_nodes', [2])
def test_new_channel(private_keys, tester_state, tester_channelmanager):
    pkey0, pkey1 = private_keys

    events = list()
    settle_timeout = 10
    channel = new_nettingcontract(
        pkey0,
        pkey1,
        tester_state,
        events.append,
        tester_channelmanager,
        settle_timeout,
    )

    assert channel.settleTimeout(sender=pkey0) == settle_timeout
    assert channel.tokenAddress(sender=pkey0) == tester_channelmanager.tokenAddress(sender=pkey0)
    assert channel.opened(sender=pkey0) == 0
    assert channel.closed(sender=pkey0) == 0
    assert channel.settled(sender=pkey0) == 0

    address_and_balances = channel.addressAndBalance(sender=pkey0)
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    assert address_and_balances[0] == encode_hex(address0)
    assert address_and_balances[1] == 0
    assert address_and_balances[2] == encode_hex(address1)
    assert address_and_balances[3] == 0


def test_deposit(private_keys, tester_channelmanager, tester_state, tester_token):
    pkey0 = private_keys[0]
    pkey1 = private_keys[1]
    address0 = encode_hex(privatekey_to_address(pkey0))
    address1 = encode_hex(privatekey_to_address(pkey1))

    settle_timeout = 10
    events = list()

    # not using the tester_nettingcontracts fixture because it has a balance set
    channel = new_nettingcontract(
        pkey0,
        pkey1,
        tester_state,
        events.append,
        tester_channelmanager,
        settle_timeout,
    )

    deposit = 100

    # cannot deposit without approving
    assert channel.deposit(deposit, sender=pkey0) is False

    assert tester_token.approve(channel.address, deposit, sender=pkey0) is True

    # cannot deposit negative values
    with pytest.raises(abi.ValueOutOfBounds):
        channel.deposit(-1, sender=pkey0)

    zero_state = (address0, 0, address1, 0)
    assert tuple(channel.addressAndBalance(sender=pkey0)) == zero_state

    assert channel.deposit(deposit, sender=pkey0) is True

    deposit_state = (address0, deposit, address1, 0)
    assert tuple(channel.addressAndBalance(sender=pkey0)) == deposit_state
    assert tester_token.balanceOf(channel.address, sender=pkey0) == deposit

    # cannot over deposit (the allowance is depleted)
    assert channel.deposit(deposit, sender=pkey0) is False

    assert tester_token.approve(channel.address, deposit, sender=pkey0) is True
    assert channel.deposit(deposit, sender=pkey0) is True

    second_deposit_state = (address0, deposit * 2, address1, 0)
    assert tuple(channel.addressAndBalance(sender=pkey0)) == second_deposit_state


def test_deposit_events(
        private_keys,
        settle_timeout,
        tester_state,
        tester_channelmanager,
        tester_token,
        tester_events):

    """ A deposit must emit the events Transfer and a ChannelNewBalance. """
    private_key = private_keys[0]
    address = privatekey_to_address(private_key)

    nettingchannel = new_nettingcontract(
        private_key,
        private_keys[1],
        tester_state,
        tester_events.append,
        tester_channelmanager,
        settle_timeout,
    )

    initial_balance0 = tester_token.balanceOf(address, sender=private_key)
    deposit_amount = initial_balance0 // 10

    assert tester_token.approve(nettingchannel.address, deposit_amount, sender=private_key) is True
    assert nettingchannel.deposit(deposit_amount, sender=private_key) is True

    transfer_event = tester_events[-2]
    newbalance_event = tester_events[-1]

    assert transfer_event == {
        '_event_type': 'Transfer',
        '_from': encode_hex(address),
        '_to': nettingchannel.address,
        '_value': deposit_amount,
    }

    block_number = tester_state.block.number
    assert newbalance_event == {
        '_event_type': 'ChannelNewBalance',
        'token_address': encode_hex(tester_token.address),
        'participant': encode_hex(address),
        'balance': deposit_amount,
        'block_number': block_number,
    }


def test_close_event(tester_state, tester_nettingcontracts, tester_events):
    """ The event ChannelClosed is emitted when close is called. """
    privatekey, _, nettingchannel = tester_nettingcontracts[0]
    address = privatekey_to_address(privatekey)

    previous_events = list(tester_events)
    nettingchannel.close('', sender=privatekey)
    assert len(previous_events) + 1 == len(tester_events)

    block_number = tester_state.block.number
    close_event = tester_events[-1]
    assert close_event == {
        '_event_type': 'ChannelClosed',
        'closing_address': encode_hex(address),
        'block_number': block_number,
    }


def test_settle_event(settle_timeout, tester_state, tester_events, tester_nettingcontracts):
    """ The event ChannelSettled is emitted when the channel is settled. """
    privatekey, _, nettingchannel = tester_nettingcontracts[0]

    nettingchannel.close('', sender=privatekey)

    tester_state.mine(number_of_blocks=settle_timeout + 1)

    previous_events = list(tester_events)
    nettingchannel.settle(sender=privatekey)

    # settle + a transfer per participant
    assert len(previous_events) + 3 == len(tester_events)

    block_number = tester_state.block.number
    settle_event = tester_events[-1]
    assert settle_event == {
        '_event_type': 'ChannelSettled',
        'block_number': block_number,
    }


def test_transfer_update_event(tester_state, tester_channels, tester_events):
    """ The event TransferUpdated is emitted when after a successful call to updateTransfer. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address1 = privatekey_to_address(pkey1)

    direct0 = make_direct_transfer(channel0, channel1, amount=90, pkey=pkey0)
    direct0_data = str(direct0.packed().data)

    nettingchannel.close('', sender=pkey0)

    previous_events = list(tester_events)
    nettingchannel.updateTransfer(direct0_data, sender=pkey1)
    assert len(previous_events) + 1 == len(tester_events)

    assert tester_events[-1] == {
        '_event_type': 'TransferUpdated',
        'node_address': address1.encode('hex'),
        'block_number': tester_state.block.number,
    }


def test_close_first_participant_can_close(tester_state, tester_nettingcontracts):
    """ First participant can close an unused channel. """
    privatekey, _, nettingchannel = tester_nettingcontracts[0]
    address = privatekey_to_address(privatekey)

    block_number = tester_state.block.number
    nettingchannel.close('', sender=privatekey)

    assert nettingchannel.closed(sender=privatekey) == block_number
    assert nettingchannel.closingAddress(sender=privatekey) == encode_hex(address)


def test_close_second_participant_can_close(tester_state, tester_nettingcontracts):
    """ Second participant can close an unused channel. """
    _, privatekey, nettingchannel = tester_nettingcontracts[0]
    address = privatekey_to_address(privatekey)

    block_number = tester_state.block.number
    nettingchannel.close('', sender=privatekey)

    assert nettingchannel.closed(sender=privatekey) == block_number
    assert nettingchannel.closingAddress(sender=privatekey) == encode_hex(address)


def test_close_only_participant_can_close(tester_nettingcontracts):
    """ Only the participants may call close. """
    # Third party close is discussed on issue #182
    _, _, nettingchannel = tester_nettingcontracts[0]

    unknown_key = tester.k3
    with pytest.raises(TransactionFailed):
        nettingchannel.close(sender=unknown_key)


def test_close_first_argument_is_for_partner_transfer(tester_channels):
    """ Close must not accept a transfer from the closing address as the first
    argument, nor a transfer of the counter party as the second.
    """
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    transfer0 = make_direct_transfer(channel0, channel1, amount=90, pkey=pkey0)
    transfer0_data = str(transfer0.packed().data)

    transfer1 = make_direct_transfer(channel1, channel0, amount=70, pkey=pkey1)
    transfer1_data = str(transfer1.packed().data)

    # a node cannot use a transfer of it's own in place of a transfer from the partner
    with pytest.raises(TransactionFailed):
        nettingchannel.close(transfer0_data, sender=pkey0)

    # and at the same pace, it cannot use a transfer of it's partner as one of it's own
    with pytest.raises(TransactionFailed):
        nettingchannel.close(transfer1_data, sender=pkey1)


def test_settle_unused_channel(
        deposit,
        settle_timeout,
        tester_state,
        tester_nettingcontracts,
        tester_token):

    """ Test settle of a channel with no transfers. """

    pkey0, pkey1, nettingchannel = tester_nettingcontracts[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey0)

    nettingchannel.close('', sender=pkey0)
    tester_state.mine(number_of_blocks=settle_timeout + 1)

    nettingchannel.settle(sender=pkey0)

    assert tester_token.balanceOf(address0, sender=pkey0) == initial_balance0 + deposit
    assert tester_token.balanceOf(address1, sender=pkey0) == initial_balance1 + deposit
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0


def test_settle_single_direct_transfer(
        deposit,
        settle_timeout,
        tester_channels,
        tester_state,
        tester_token):

    """ Test settle of a channel with uni-directional transfers. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial0 = tester_token.balanceOf(address0, sender=pkey0)
    initial1 = tester_token.balanceOf(address1, sender=pkey0)

    amount = 90
    transfer0 = make_direct_transfer(channel0, channel1, amount, pkey0)
    transfer0_data = str(transfer0.packed().data)

    nettingchannel.close(transfer0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    assert tester_token.balanceOf(address0, sender=pkey0) == initial0 + deposit - amount
    assert tester_token.balanceOf(address1, sender=pkey0) == initial1 + deposit + amount
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0


def test_settle_two_direct_transfers(
        deposit,
        settle_timeout,
        tester_state,
        tester_channels,
        tester_token):

    """ Test settle of a channel with bi-directional transfers. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey0)

    amount0 = 10
    transfer0 = make_direct_transfer(channel0, channel1, amount0, pkey0)
    transfer0_data = str(transfer0.packed().data)

    amount1 = 30
    transfer1 = make_direct_transfer(channel1, channel0, amount1, pkey1)
    transfer1_data = str(transfer1.packed().data)

    nettingchannel.close(transfer1_data, sender=pkey0)
    nettingchannel.updateTransfer(transfer0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    balance0 = tester_token.balanceOf(address0, sender=pkey0)
    balance1 = tester_token.balanceOf(address1, sender=pkey0)
    assert balance0 == initial_balance0 + deposit - amount0 + amount1
    assert balance1 == initial_balance1 + deposit + amount0 - amount1
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0


def test_update_unclosed_channel(tester_channels):
    """ Cannot call updateTransfer on a open channel. """
    pkey0, _, nettingchannel, channel0, channel1 = tester_channels[0]

    transfer0 = make_direct_transfer(channel0, channel1, amount=10, pkey=pkey0)
    transfer0_data = str(transfer0.packed().data)

    with pytest.raises(TransactionFailed):
        nettingchannel.updateTransfer(transfer0_data, sender=pkey0)


def test_update_not_allowed_after_settlement_period(settle_timeout, tester_channels, tester_state):
    """ updateTransfer cannot be called after the settlement period. """
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    direct0 = make_direct_transfer(channel0, channel1, amount=70, pkey=pkey0)
    direct0_data = str(direct0.packed().data)

    nettingchannel.close('', sender=pkey0)
    tester_state.mine(number_of_blocks=settle_timeout + 1)

    # reject the older transfer
    with pytest.raises(TransactionFailed):
        nettingchannel.updateTransfer(direct0_data, sender=pkey1)


def test_update_closing_address(tester_channels):
    """ Closing address cannot call updateTransfer. """
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    transfer0 = make_direct_transfer(channel0, channel1, amount=10, pkey=pkey0)
    transfer0_data = str(transfer0.packed().data)

    transfer1 = make_direct_transfer(channel1, channel0, amount=10, pkey=pkey1)
    transfer1_data = str(transfer1.packed().data)

    nettingchannel.close('', sender=pkey0)

    # do not accept a transfer of it's own
    with pytest.raises(TransactionFailed):
        nettingchannel.updateTransfer(transfer0_data, sender=pkey0)

    # nor a transfer from the partner
    with pytest.raises(TransactionFailed):
        nettingchannel.updateTransfer(transfer1_data, sender=pkey0)


@pytest.mark.parametrize('both_participants_deposit', [False])
@pytest.mark.parametrize('deposit', [100])
def test_update_single_direct_transfer(
        deposit,
        settle_timeout,
        tester_state,
        tester_channels,
        tester_token):

    """ Test the updateTransfer when the closing party hasn't provided the courtesy transfer. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial0 = tester_token.balanceOf(address0, sender=pkey0)
    initial1 = tester_token.balanceOf(address1, sender=pkey0)

    amount0 = 90
    direct0 = make_direct_transfer(channel0, channel1, amount0, pkey0)

    # close without providing the courtesy
    nettingchannel.close('', sender=pkey0)

    # update the missing transfer
    direct_from0 = str(direct0.packed().data)
    nettingchannel.updateTransfer(direct_from0, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey1)

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == initial0 + deposit - amount0
    assert tester_token.balanceOf(address1, sender=pkey0) == initial1 + amount0


def test_update_two_direct_transfer(
        settle_timeout,
        deposit,
        tester_state,
        tester_channels,
        tester_token):

    """ Test the updateTransfer when the closing party hasn't provided the
    courtesy transfer but has provided one received transfer.
    """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial0 = tester_token.balanceOf(address0, sender=pkey0)
    initial1 = tester_token.balanceOf(address1, sender=pkey0)

    amount0 = 90
    direct0 = make_direct_transfer(channel0, channel1, amount0, pkey0)

    amount1 = 90
    direct1 = make_direct_transfer(channel1, channel0, amount1, pkey1)

    direct_from0 = str(direct0.packed().data)
    direct_from1 = str(direct1.packed().data)

    # close without providing the courtesy
    nettingchannel.close(direct_from1, sender=pkey0)

    # update the missing transfer
    nettingchannel.updateTransfer(direct_from0, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey1)

    balance0 = initial0 + deposit - amount0 + amount1
    balance1 = initial1 + deposit + amount0 - amount1

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey0) == balance1


@pytest.mark.parametrize('both_participants_deposit', [False])
@pytest.mark.parametrize('deposit', [100])
def test_update_with_locked_mediated_transfer(
        deposit,
        settle_timeout,
        reveal_timeout,
        tester_state,
        tester_channels,
        tester_token):

    """ Test the updateTransfer when the closing party hasn't provided the courtesy transfer. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial0 = tester_token.balanceOf(address0, sender=pkey0)
    initial1 = tester_token.balanceOf(address1, sender=pkey0)

    transferred_amount0 = 30
    increase_transferred_amount(channel0, channel1, transferred_amount0)

    expiration0 = tester_state.block.number + reveal_timeout + 5
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock0 = Lock(amount=29, expiration=expiration0, hashlock=sha3('lock1'))
    mediated = make_mediated_transfer(
        channel0,
        channel1,
        address0,
        address1,
        lock0,
        pkey0,
        tester_state.block.number,
    )

    # close without providing the courtesy
    nettingchannel.close('', sender=pkey0)

    # update the missing transfer
    transfer_data = str(mediated.packed().data)
    nettingchannel.updateTransfer(transfer_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey1)

    # the balances only change by transferred_amount because the lock was /not/ unlocked
    balance0 = initial0 + deposit - transferred_amount0
    balance1 = initial1 + transferred_amount0

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey0) == balance1


def test_two_locked_mediated_transfer_messages(
        deposit,
        settle_timeout,
        reveal_timeout,
        tester_state,
        tester_channels,
        tester_token):

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey1)

    transferred_amount0 = 30
    increase_transferred_amount(channel0, channel1, transferred_amount0)

    transferred_amount1 = 70
    increase_transferred_amount(channel1, channel0, transferred_amount1)

    expiration0 = tester_state.block.number + reveal_timeout + 5
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock0 = Lock(amount=29, expiration=expiration0, hashlock=sha3('lock1'))
    mediated0 = make_mediated_transfer(
        channel0,
        channel1,
        address0,
        address1,
        lock0,
        pkey0,
        tester_state.block.number,
    )
    mediated0_data = str(mediated0.packed().data)

    lock_expiration1 = tester_state.block.number + reveal_timeout + 5
    lock1 = Lock(amount=31, expiration=lock_expiration1, hashlock=sha3('lock2'))
    mediated1 = make_mediated_transfer(
        channel1,
        channel0,
        address1,
        address0,
        lock1,
        pkey1,
        tester_state.block.number,
    )
    mediated1_data = str(mediated1.packed().data)

    nettingchannel.close(mediated0_data, sender=pkey1)
    nettingchannel.updateTransfer(mediated1_data, sender=pkey0)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    # the balances only change by transferred_amount because the lock was /not/ unlocked
    balance0 = initial_balance0 + deposit - transferred_amount0 + transferred_amount1
    balance1 = initial_balance1 + deposit + transferred_amount0 - transferred_amount1

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey1) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey1) == balance1


def test_dispute_one_direct_transfer(
        settle_timeout,
        deposit,
        tester_state,
        tester_channels,
        tester_token):

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial0 = tester_token.balanceOf(address0, sender=pkey0)
    initial1 = tester_token.balanceOf(address1, sender=pkey0)

    first_amount0 = 90
    first_direct0 = make_direct_transfer(channel0, channel1, first_amount0, pkey0)
    first_direct0_data = str(first_direct0.packed().data)

    second_amount0 = 90
    second_direct0 = make_direct_transfer(channel0, channel1, second_amount0, pkey0)
    second_direct0_data = str(second_direct0.packed().data)

    # provide the wrong transfer
    nettingchannel.close('', sender=pkey0)

    # update the wrong transfer
    nettingchannel.updateTransfer(second_direct0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    balance0 = initial0 + deposit - first_amount0 - second_amount0
    balance1 = initial1 + deposit + first_amount0 + second_amount0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey0) == balance1
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0


def test_dispute_mediated_on_top_of_direct(
        reveal_timeout,
        settle_timeout,
        deposit,
        tester_state,
        tester_channels,
        tester_token):

    """ The transfer types must not change the behavior of the dispute. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey0)

    first_amount0 = 90
    first_direct0 = make_direct_transfer(channel0, channel1, first_amount0, pkey0)
    first_direct0_data = str(first_direct0.packed().data)

    lock_expiration = tester_state.block.number + reveal_timeout + 5
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock1 = Lock(amount=31, expiration=lock_expiration, hashlock=sha3('lock2'))
    second_mediated0 = make_mediated_transfer(
        channel0,
        channel1,
        address0,
        address1,
        lock1,
        pkey0,
        tester_state.block.number,
    )
    second_mediated0_data = str(second_mediated0.packed().data)

    nettingchannel.close('', sender=pkey0)
    nettingchannel.updateTransfer(second_mediated0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    # the balances only change by transferred_amount because the lock was /not/ unlocked
    balance0 = initial_balance0 + deposit - first_amount0
    balance1 = initial_balance1 + deposit + first_amount0

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey1) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey1) == balance1


def test_dispute_ignore_older_transfers(tester_channels):
    """ updateTransfer must not accept older transfers. """
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    direct1 = make_direct_transfer(channel1, channel0, amount=30, pkey=pkey1)
    direct1_data = str(direct1.packed().data)

    first_direct0 = make_direct_transfer(channel0, channel1, amount=70, pkey=pkey0)
    first_direct0_data = str(first_direct0.packed().data)

    second_direct0 = make_direct_transfer(channel0, channel1, amount=90, pkey=pkey0)
    second_direct0_data = str(second_direct0.packed().data)

    # provide the newest transfer
    nettingchannel.close(direct1_data, sender=pkey0)
    nettingchannel.updateTransfer(second_direct0_data, sender=pkey1)

    # reject the older transfer
    with pytest.raises(TransactionFailed):
        nettingchannel.updateTransfer(first_direct0_data, sender=pkey1)


def test_unlock(
        deposit,
        settle_timeout,
        reveal_timeout,
        tester_channels,
        tester_state,
        tester_token):

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey0)

    lock_amount = 31
    lock_expiration = tester_state.block.number + reveal_timeout + 5
    secret = 'secretsecretsecretsecretsecretse'
    hashlock = sha3(secret)
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock0 = Lock(lock_amount, lock_expiration, hashlock)

    mediated0 = make_mediated_transfer(
        channel0,
        channel1,
        address0,
        address1,
        lock0,
        pkey0,
        tester_state.block.number,
        secret,
    )
    mediated0_data = str(mediated0.packed().data)

    proof = channel1.our_state.balance_proof.compute_proof_for_lock(
        secret,
        mediated0.lock,
    )

    nettingchannel.close(mediated0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=1)

    nettingchannel.unlock(
        proof.lock_encoded,
        ''.join(proof.merkle_proof),
        proof.secret,
        sender=pkey1,
    )

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    balance0 = initial_balance0 + deposit - lock0.amount
    balance1 = initial_balance1 + deposit + lock0.amount
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey0) == balance1
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey0) == 0


def test_unlock_expired_lock(reveal_timeout, tester_channels, tester_state):
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    lock_timeout = reveal_timeout + 5
    lock_expiration = tester_state.block.number + lock_timeout
    secret = 'expiredlockexpiredlockexpiredloc'
    hashlock = sha3(secret)
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock1 = Lock(amount=31, expiration=lock_expiration, hashlock=hashlock)

    mediated0 = make_mediated_transfer(
        channel1,
        channel0,
        privatekey_to_address(pkey0),
        privatekey_to_address(pkey1),
        lock1,
        pkey1,
        tester_state.block.number,
        secret,
    )
    mediated0_data = str(mediated0.packed().data)

    nettingchannel.close(mediated0_data, sender=pkey0)

    # expire the lock
    tester_state.mine(number_of_blocks=lock_timeout + 1)

    unlock_proofs = list(channel0.our_state.balance_proof.get_known_unlocks())
    proof = unlock_proofs[0]

    with pytest.raises(TransactionFailed):
        nettingchannel.unlock(
            proof.lock_encoded,
            ''.join(proof.merkle_proof),
            proof.secret,
            sender=pkey0,
        )


def test_unlock_twice(reveal_timeout, tester_channels, tester_state):
    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    lock_expiration = tester_state.block.number + reveal_timeout + 5
    secret = 'secretsecretsecretsecretsecretse'
    new_block = Block(tester_state.block.number)
    channel0.state_transition(new_block)
    channel1.state_transition(new_block)
    lock = Lock(17, lock_expiration, sha3(secret))

    mediated0 = make_mediated_transfer(
        channel1,
        channel0,
        privatekey_to_address(pkey1),
        privatekey_to_address(pkey0),
        lock,
        pkey1,
        tester_state.block.number,
        secret,
    )
    mediated0_data = str(mediated0.packed().data)

    unlock_proofs = list(channel0.our_state.balance_proof.get_known_unlocks())
    assert len(unlock_proofs) == 1
    proof = unlock_proofs[0]

    nettingchannel.close(mediated0_data, sender=pkey0)

    nettingchannel.unlock(
        proof.lock_encoded,
        ''.join(proof.merkle_proof),
        proof.secret,
        sender=pkey0,
    )

    with pytest.raises(TransactionFailed):
        nettingchannel.unlock(
            proof.lock_encoded,
            ''.join(proof.merkle_proof),
            proof.secret,
            sender=pkey0,
        )


def test_settlement_with_unauthorized_token_transfer(
        deposit,
        settle_timeout,
        tester_state,
        tester_channels,
        tester_token):

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]

    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey0)

    amount0 = 10
    transfer0 = make_direct_transfer(channel0, channel1, amount0, pkey0)
    transfer0_data = str(transfer0.packed().data)

    amount1 = 30
    transfer1 = make_direct_transfer(channel1, channel0, amount1, pkey1)
    transfer1_data = str(transfer1.packed().data)

    extra_amount = 10
    assert tester_token.transfer(nettingchannel.address, extra_amount, sender=pkey0)

    nettingchannel.close(transfer1_data, sender=pkey0)
    nettingchannel.updateTransfer(transfer0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    balance0 = tester_token.balanceOf(address0, sender=pkey0)
    balance1 = tester_token.balanceOf(address1, sender=pkey0)
    assert balance0 == initial_balance0 + deposit - amount0 + amount1
    assert balance1 == initial_balance1 + deposit + amount0 - amount1

    # Make sure that the extra amount is burned/locked in the netting channel
    assert tester_token.balanceOf(nettingchannel.address, sender=pkey1) == extra_amount


def test_netting(deposit, settle_timeout, tester_channels, tester_state, tester_token):
    """ Transferred amount can be larger than the deposit. """

    pkey0, pkey1, nettingchannel, channel0, channel1 = tester_channels[0]
    address0 = privatekey_to_address(pkey0)
    address1 = privatekey_to_address(pkey1)

    initial_balance0 = tester_token.balanceOf(address0, sender=pkey0)
    initial_balance1 = tester_token.balanceOf(address1, sender=pkey1)

    transferred_amount0 = deposit * 3 + 30
    increase_transferred_amount(channel0, channel1, transferred_amount0)

    transferred_amount1 = deposit * 3 + 70
    increase_transferred_amount(channel1, channel0, transferred_amount1)

    amount0 = 10
    transferred_amount0 += amount0
    direct0 = make_direct_transfer(channel0, channel1, amount0, pkey0)
    direct0_data = str(direct0.packed().data)

    amount1 = 30
    transferred_amount1 += amount1
    direct1 = make_direct_transfer(channel1, channel0, amount1, pkey1)
    direct1_data = str(direct1.packed().data)

    nettingchannel.close(direct1_data, sender=pkey0)
    nettingchannel.updateTransfer(direct0_data, sender=pkey1)

    tester_state.mine(number_of_blocks=settle_timeout + 1)
    nettingchannel.settle(sender=pkey0)

    # the balances only change by transferred_amount because the lock was /not/ unlocked
    balance0 = initial_balance0 + deposit - transferred_amount0 + transferred_amount1
    balance1 = initial_balance1 + deposit + transferred_amount0 - transferred_amount1

    assert tester_token.balanceOf(nettingchannel.address, sender=pkey1) == 0
    assert tester_token.balanceOf(address0, sender=pkey0) == balance0
    assert tester_token.balanceOf(address1, sender=pkey1) == balance1
