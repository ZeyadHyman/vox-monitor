import dataclasses

import pytest

from vox_monitor.config import ConfigError, Settings, showtimes_url


def base_env(**overrides):
    env = {
        "TELEGRAM_BOT_TOKEN": "t",
        "TELEGRAM_CHAT_ID": "c",
        "UPSTASH_REDIS_REST_URL": "https://redis.example/",
        "UPSTASH_REDIS_REST_TOKEN": "r",
        "MONITOR_SECRET": "s",
    }
    env.update(overrides)
    return env


def test_from_env_parses_settings():
    s = Settings.from_env(base_env(DRY_RUN="1"))
    assert s.dry_run is True
    assert s.redis_url == "https://redis.example"  # trailing slash stripped
    assert s.monitor_secret == "s"


def test_cinema_scope_cannot_be_widened_by_env():
    """CINEMA_SLUGS must no longer exist: scope is hard-coded to Almaza."""
    s = Settings.from_env(base_env(CINEMA_SLUGS="mall-of-egypt,city-centre-alexandria"))
    field_names = {f.name for f in dataclasses.fields(Settings)}
    assert "cinema_slugs_override" not in field_names
    assert not hasattr(s, "cinema_slugs_override")


def test_missing_env_lists_names_not_values():
    with pytest.raises(ConfigError) as exc:
        Settings.from_env({"TELEGRAM_BOT_TOKEN": "secret-value"})
    msg = str(exc)
    assert "TELEGRAM_CHAT_ID" in msg
    assert "UPSTASH_REDIS_REST_URL" in msg
    assert "secret-value" not in msg


def test_bad_int_env_raises():
    with pytest.raises(ConfigError):
        Settings.from_env(base_env(MAX_WORKERS="many"))


def test_showtimes_url_has_no_movie_params():
    # No m= params: VOX then renders EVERY movie at the cinema, which is
    # what makes automatic all-movie discovery work.
    url = showtimes_url("city-centre-almaza", "20261223")
    assert url == "https://egy.voxcinemas.com/showtimes?c=city-centre-almaza&d=20261223"
    assert "m=" not in url


def test_showtimes_url_without_date():
    url = showtimes_url("city-centre-almaza")
    assert url == "https://egy.voxcinemas.com/showtimes?c=city-centre-almaza"
    assert "m=" not in url
    assert "d=" not in url


def test_slug_validator_removed_with_discovery():
    # No user-supplied slugs remain (single hard-coded cinema), so the
    # validation helper went away with discovery.
    import vox_monitor.config as config_module

    assert not hasattr(config_module, "is_safe_slug")


def test_secret_comparison_uses_constant_time():
    import hmac
    import inspect

    from api import check as check_module

    source = inspect.getsource(check_module._secret_ok)
    assert "compare_digest" in source
    assert hmac.compare_digest(b"a", b"a")
