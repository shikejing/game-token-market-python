def test_modules_import():
    import main  # noqa: F401
    from app import api, engine, models, service, storage  # noqa: F401
