import pytest

from router.routing.route_set import RouteSetInvariantError, route_set_indices
from router.routing.tree import tree_from_dict
from tests.routing_fixtures import tree_dict


def enabled(tree_id, *, default=False, departments=()):
    return tree_from_dict(tree_dict(
        tree_id, enabled=True, default=default, departments=list(departments)))


def test_route_set_indices_maps_departments_and_default():
    default, mapping = route_set_indices([
        enabled('main', default=True),
        enabled('finance', departments=['财务部']),
    ])
    assert default == 'main'
    assert mapping == {'财务部': 'finance'}


def test_route_set_rejects_duplicate_department_and_missing_default():
    with pytest.raises(RouteSetInvariantError) as conflict:
        route_set_indices([
            enabled('main', default=True, departments=['财务部']),
            enabled('other', departments=['财务部']),
        ])
    assert conflict.value.code == 'department_conflict'
    assert conflict.value.department == '财务部'
    assert conflict.value.tree_ids == ('main', 'other')
    with pytest.raises(RouteSetInvariantError) as missing:
        route_set_indices([enabled('main', departments=['财务部'])])
    assert missing.value.code == 'no_default_tree'
