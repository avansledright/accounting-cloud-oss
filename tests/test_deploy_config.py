"""Schedules (17:00 prices, report times) and "today" follow TZ, so the image
must ship zoneinfo and default to a zone the user can override."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts)) as fh:
        return fh.read()


def test_image_installs_tzdata_so_tz_is_honoured():
    dockerfile = _read('Dockerfile')
    assert 'tzdata' in dockerfile
    # A neutral, overridable default: no deployment's zone is baked in.
    assert re.search(r'^ENV TZ=UTC$', dockerfile, re.M)


def test_kubernetes_config_documents_tz():
    configmap = _read('deploy', 'kubernetes', 'configmap.yaml')
    assert re.search(r'^\s*#\s*TZ:', configmap, re.M)
