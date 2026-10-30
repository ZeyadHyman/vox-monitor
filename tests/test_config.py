import pytest

from vox_monitor.config import ConfigError, Settings, is_safe_slug, showtimes_url


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
    s = Settings.from_env(base_env(DRY_RUN="1", CINEMA_SLUGS="a-b,c-d"))
    assert s.dry_run is True
    assert s.cinema_slugs_override == ("a-b", "c-d")
    assert s.redis_url == "https://redis.example"  # trailing slash stripped
    assert s.monitor_secret == "s"


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


def test_showtimes_url_builds_query():
    url = showtimes_url(
        "city-centre-almaza",
        ("avengers-doomsday", "avengers-doomsday-infinity-vision"),
        "20261223",
    )
    assert url == (
        "https://egy.voxcinemas.com/showtimes?c=city-centre-almaza"
        "&m=avengers-doomsday&m=avengers-doomsday-infinity-vision&d=20261223"
    )


def test_showtimes_url_without_date():
    url = showtimes_url("mall-of-egypt", ("avengers-doomsday",))
    assert url == "https://egy.voxcinemas.com/showtimes?c=mall-of-egypt&m=avengers-doomsday"


def test_is_safe_slug():
    assert is_safe_slug("city-centre-almaza")
    assert not is_safe_slug("../etc")
    assert not is_safe_slug("")


def test_secret_comparison_uses_constant_time():
    import hmac
    import inspect

    from api import check as check_module

    source = inspect.getsource(check_module._secret_ok)
    assert "compare_digest" in source
    assert hmac.compare_digest(b"a", b"a")
