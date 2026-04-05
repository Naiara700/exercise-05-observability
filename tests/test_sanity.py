import yaml

def test_compose_has_five_services():
    with open("docker-compose.yml") as f:
        c = yaml.safe_load(f)
    assert len(c.get("services", {})) >= 5