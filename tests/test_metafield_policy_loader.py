from src.config.metafield_policy_loader import load_metafield_policy


def test_load_metafield_policy_from_repo_yaml():
    policy = load_metafield_policy("src/config/metafield_translation.yaml")
    assert "product_handle" in policy.blocked_leaf_names
    assert "custom.accessori" in policy.allowed_leafs_by_key
    assert "title" in policy.allowed_leafs_by_key["custom.accessori"]
