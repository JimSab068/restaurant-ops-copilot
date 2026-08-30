import pytest

import seed


def test_seed_creates_all_configured_tenants(monkeypatch):
    committed = {"value": False}
    created = []

    class FakeSession:
        def add(self, obj):
            created.append(obj)

        def add_all(self, objects):
            created.extend(objects)

        def flush(self):
            return None

        def commit(self):
            committed["value"] = True

        def rollback(self):
            raise AssertionError("rollback should not occur")

        def close(self):
            pass

    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr(seed, "get_session", lambda: FakeSession())

    # This test exercises validation/transaction orchestration rather than
    # relying on the real database model implementation.
    result = seed.seed()

    assert committed["value"] is True
    assert len(result) == len(seed.TENANT_CONFIGS)


def test_seed_rolls_back_on_database_failure(monkeypatch):
    rollback_called = {"value": False}

    class FakeSession:
        def add(self, obj):
            pass

        def add_all(self, objects):
            pass

        def flush(self):
            raise RuntimeError("database failure")

        def commit(self):
            raise AssertionError("commit must not occur")

        def rollback(self):
            rollback_called["value"] = True

        def close(self):
            pass

    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr(seed, "get_session", lambda: FakeSession())

    with pytest.raises(seed.SeedError, match="failed to seed"):
        seed.seed()

    assert rollback_called["value"] is True


def test_seed_always_closes_session(monkeypatch):
    closed = {"value": False}

    class FakeSession:
        def add(self, obj):
            pass

        def add_all(self, objects):
            pass

        def flush(self):
            raise RuntimeError("database failure")

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            closed["value"] = True

    monkeypatch.setattr(seed, "init_db", lambda: None)
    monkeypatch.setattr(seed, "get_session", lambda: FakeSession())

    with pytest.raises(seed.SeedError):
        seed.seed()

    assert closed["value"] is True


def test_empty_configuration_is_rejected(monkeypatch):
    monkeypatch.setattr(seed, "TENANT_CONFIGS", ())

    with pytest.raises(seed.SeedError, match="must not be empty"):
        seed.seed()


def test_duplicate_tenant_names_are_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    monkeypatch.setattr(
        seed,
        "TENANT_CONFIGS",
        (config, config),
    )

    with pytest.raises(seed.SeedError, match="duplicate tenant"):
        seed.seed()


def test_tenant_without_supplier_is_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=(),
        ingredients=config.ingredients,
        menu_items=config.menu_items,
    )

    monkeypatch.setattr(
        seed,
        "TENANT_CONFIGS",
        (invalid,),
    )

    with pytest.raises(seed.SeedError, match="at least one supplier"):
        seed.seed()


@pytest.mark.parametrize("price", [0, -1, -100])
def test_invalid_ingredient_price_is_rejected(monkeypatch, price):
    config = seed.TENANT_CONFIGS[0]

    invalid_ingredient = seed.IngredientConfig(
        name="Invalid",
        unit="kg",
        price=price,
        stock=10,
    )

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=config.suppliers,
        ingredients=(invalid_ingredient,),
        menu_items=config.menu_items,
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="ingredient price must be greater than zero",
    ):
        seed.seed()


def test_negative_stock_is_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=config.suppliers,
        ingredients=(
            seed.IngredientConfig(
                name="Invalid",
                unit="kg",
                price=1.0,
                stock=-1,
            ),
        ),
        menu_items=config.menu_items,
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="stock cannot be negative",
    ):
        seed.seed()


@pytest.mark.parametrize("price", [0, -1])
def test_invalid_menu_price_is_rejected(monkeypatch, price):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=config.suppliers,
        ingredients=config.ingredients,
        menu_items=(
            seed.MenuItemConfig(
                name="Invalid",
                price=price,
            ),
        ),
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="menu item price must be greater than zero",
    ):
        seed.seed()


def test_duplicate_supplier_names_are_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=("Sysco", "Sysco"),
        ingredients=config.ingredients,
        menu_items=config.menu_items,
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="duplicate supplier",
    ):
        seed.seed()


def test_duplicate_menu_items_are_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=config.suppliers,
        ingredients=config.ingredients,
        menu_items=(
            seed.MenuItemConfig("Pizza", 10.0),
            seed.MenuItemConfig("Pizza", 12.0),
        ),
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="duplicate menu item",
    ):
        seed.seed()


def test_duplicate_ingredients_are_rejected(monkeypatch):
    config = seed.TENANT_CONFIGS[0]

    invalid = seed.TenantConfig(
        name=config.name,
        suppliers=config.suppliers,
        ingredients=(
            seed.IngredientConfig("Flour", "kg", 1.0, 10),
            seed.IngredientConfig("Flour", "kg", 2.0, 20),
        ),
        menu_items=config.menu_items,
    )

    monkeypatch.setattr(seed, "TENANT_CONFIGS", (invalid,))

    with pytest.raises(
        seed.SeedError,
        match="duplicate ingredient",
    ):
        seed.seed()