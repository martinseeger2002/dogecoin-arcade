"""Pepecoin's soft dust rule, priced (fees.soft_dust_fee, funding.price).

Every spendable output under 0.01 costs 0.01 more in fee or peers refuse the
transaction; our own node takes it anyway, so without this it waits for a block
for ever -- the answered-offer swap of 2026-09-26 did, with its 0.00589
reservation paid back to the seller.
"""

from arcade import fees, funding


P2PKH = bytes.fromhex("76a914" + "11" * 20 + "88ac")
OP_RETURN = bytes.fromhex("6a04deadbeef")


def test_an_output_under_a_cent_costs_a_cent_more():
    assert fees.soft_dust_fee([(589_000, P2PKH)]) == fees.DUST_LIMIT
    assert fees.soft_dust_fee([(fees.DUST_LIMIT, P2PKH)]) == 0, "0.01 itself is fine"
    assert fees.soft_dust_fee([(0, OP_RETURN)]) == 0, "unspendable data is not dust"
    assert fees.soft_dust_fee([(1, P2PKH), (2, P2PKH)]) == 2 * fees.DUST_LIMIT


def test_price_includes_it():
    plain = funding.price(2, [(0, OP_RETURN), (5 * 10**8, P2PKH)], fees.MIN_FEE_PER_KB, change=True)
    dusty = funding.price(2, [(0, OP_RETURN), (5 * 10**8, P2PKH), (589_000, P2PKH)],
                          fees.MIN_FEE_PER_KB, change=True)
    assert dusty - plain >= fees.DUST_LIMIT
