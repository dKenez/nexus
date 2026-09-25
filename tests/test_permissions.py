from nexus.bot.permissions import RoleTiers, Tier

tiers = RoleTiers.of(viewer=[1], operator=[2], admin=[3])


def test_highest_tier_wins() -> None:
    assert tiers.tier_of([1, 2, 3]) is Tier.ADMIN
    assert tiers.tier_of([1, 2]) is Tier.OPERATOR
    assert tiers.tier_of([1]) is Tier.VIEWER


def test_no_matching_role() -> None:
    assert tiers.tier_of([99]) is None
    assert tiers.tier_of([]) is None


def test_empty_viewer_list_lets_everyone_view() -> None:
    open_tiers = RoleTiers.of(viewer=[], operator=[2], admin=[3])
    assert open_tiers.tier_of([]) is Tier.VIEWER
    assert open_tiers.tier_of([2]) is Tier.OPERATOR


def test_tiers_are_ordered() -> None:
    assert Tier.VIEWER < Tier.OPERATOR < Tier.ADMIN
