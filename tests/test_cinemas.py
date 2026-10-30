"""Monitoring scope tests: City Centre Almaza only, nothing else."""

from vox_monitor.cinemas import MONITORED_CINEMA


def test_single_monitored_cinema_is_almaza():
    assert MONITORED_CINEMA.slug == "city-centre-almaza"
    assert MONITORED_CINEMA.name == "City Centre Almaza"


def test_no_other_cinema_constants_exist():
    import vox_monitor.cinemas as cinemas_module

    exported = {
        name
        for name in dir(cinemas_module)
        if not name.startswith("_") and name != "annotations"
    }
    # Only the single monitored cinema (plus module metadata) is exported —
    # there is no fallback list that could widen the scope.
    assert exported == {"MONITORED_CINEMA", "Cinema"}
    assert cinemas_module.MONITORED_CINEMA.slug != "mall-of-egypt"
    assert cinemas_module.MONITORED_CINEMA.slug != "city-centre-alexandria"


def test_cinema_discovery_removed_entirely():
    import vox_monitor.cinemas as cinemas_module

    assert not hasattr(cinemas_module, "discover_cinemas")
    assert not hasattr(cinemas_module, "FALLBACK_CINEMAS")

    import vox_monitor.showtimes as showtimes_module

    assert not hasattr(showtimes_module, "parse_cinemas_page")
