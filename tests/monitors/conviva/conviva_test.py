"""
Tests for the conviva monitor
"""

# pylint: disable=redefined-outer-name
import json
import os
import re
import time
from functools import partial as p
from textwrap import dedent

import pytest
import requests

from tests.helpers.agent import Agent
from tests.helpers.assertions import (
    all_datapoints_have_dim_key,
    all_datapoints_have_dims,
    all_datapoints_have_metric_name,
    all_datapoints_have_metric_name_and_dims,
    has_datapoint,
    has_datapoint_with_dim_key,
    has_datapoint_with_metric_name,
)
from tests.helpers.util import ensure_always, wait_for

pytestmark = [pytest.mark.conviva, pytest.mark.monitor_without_endpoints]

CONVIVA_PULSE_API_URL = os.environ.get("CONVIVA_PULSE_API_URL", "https://api.conviva.com/insights/2.4/")
CONVIVA_PULSE_USERNAME = os.environ.get("CONVIVA_PULSE_USERNAME")
CONVIVA_PULSE_PASSWORD = os.environ.get("CONVIVA_PULSE_PASSWORD")
if not CONVIVA_PULSE_USERNAME or not CONVIVA_PULSE_PASSWORD:
    pytest.skip("CONVIVA_PULSE_USERNAME and/or CONVIVA_PULSE_PASSWORD env vars not set", allow_module_level=True)
CONVIVA_DEBUG = False
if os.environ.get("CONVIVA_DEBUG", "").lower() in ["1", "yes", "true"]:
    CONVIVA_DEBUG = True


def get_conviva_json(path, max_attempts=3):
    url = CONVIVA_PULSE_API_URL.rstrip("/") + "/" + path.lstrip("/")
    auth = requests.auth.HTTPBasicAuth(CONVIVA_PULSE_USERNAME, CONVIVA_PULSE_PASSWORD)
    json_resp = None
    attempts = 0
    while attempts < max_attempts:
        with requests.get(url, auth=auth) as resp:
            if resp.status_code == 200:
                json_resp = json.loads(resp.text)
                break
        time.sleep(5)
        attempts += 1
    return json_resp


@pytest.fixture(scope="module")
def conviva_accounts():
    accounts_json = get_conviva_json("accounts.json")
    assert (
        "accounts" in accounts_json.keys() and accounts_json["accounts"].keys()
    ), "No accounts found in accounts.json response:\n%s" % str(accounts_json)
    return list(accounts_json["accounts"].keys())


@pytest.fixture(scope="module")
def conviva_filters():
    filters_json = get_conviva_json("filters.json")
    assert filters_json, "No filters found in filters.json response:\n%s" % str(filters_json)
    return list(filters_json.values())


@pytest.fixture(scope="module")
def conviva_metriclens_dimensions():
    metriclens_dimensions_json = get_conviva_json("metriclens_dimension_list.json")
    assert metriclens_dimensions_json, (
        "No metriclens dimensions found in metriclens_dimension_list.json response:\n%s" % metriclens_dimensions_json
    )
    return list(metriclens_dimensions_json.keys())


def get_dim_key(metriclens_dimension):
    return re.sub(r"\W", "_", metriclens_dimension)


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_basic():
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
    """
        ),
        debug=False,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        pattern = re.compile(r"^conviva\.quality_metriclens\..*")
        assert ensure_always(
            p(all_datapoints_have_metric_name_and_dims, agent.fake_services, pattern, {"filter": "All Traffic"})
        ), "Received datapoints without metric quality_metriclens or {filter: All Traffic} dimension"
        agent_status = agent.current_status_text
        assert CONVIVA_PULSE_PASSWORD not in agent_status, (
            "cleartext password(s) found in agent status output!\n\n%s\n" % agent_status
        )
        assert CONVIVA_PULSE_PASSWORD not in agent.output, (
            "cleartext password(s) found in agent output!\n\n%s\n" % agent.output
        )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_extra_dimensions():
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          extraDimensions:
            metric_source: conviva
            mydim: foo
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        assert ensure_always(
            p(all_datapoints_have_dims, agent.fake_services, {"metric_source": "conviva", "mydim": "foo"})
        ), "Received conviva datapoints without extra dimensions"


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_single_metric():
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        assert ensure_always(
            p(all_datapoints_have_metric_name, agent.fake_services, "conviva.concurrent_plays")
        ), "Received conviva datapoints for other metrics"


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_multi_metric():
    with Agent.run(
        dedent(
            f"""
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
          - metricParameter: plays
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(
            p(has_datapoint_with_metric_name, agent.fake_services, "conviva.concurrent_plays"), 60
        ), "Didn't get conviva datapoints for metric concurrent_plays"
        assert wait_for(
            p(has_datapoint_with_metric_name, agent.fake_services, "conviva.plays"), 60
        ), "Didn't get conviva datapoints for metric plays"


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_metriclens():
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: audience_metriclens
          - metricParameter: quality_metriclens
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        pattern = re.compile(r"^conviva\.audience_metriclens\..*")
        assert wait_for(
            p(has_datapoint_with_metric_name, agent.fake_services, pattern), 60
        ), "Didn't get conviva datapoints for metriclens audience_metriclens"
        pattern = re.compile(r"^conviva\.quality_metriclens\..*")
        assert wait_for(
            p(has_datapoint_with_metric_name, agent.fake_services, pattern), 60
        ), "Didn't get conviva datapoints for metriclens quality_metriclens"


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_single_metriclens_dimension(conviva_metriclens_dimensions):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: quality_metriclens
            metricLensDimensions:
            - {conviva_metriclens_dimensions[0]}
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        pattern = re.compile(r"^conviva\.quality_metriclens\..*")
        assert ensure_always(
            p(all_datapoints_have_metric_name, agent.fake_services, pattern)
        ), "Received conviva datapoints for other metrics"
        assert ensure_always(
            p(all_datapoints_have_dim_key, agent.fake_services, get_dim_key(conviva_metriclens_dimensions[0]))
        ), ("Received conviva datapoints without %s dimension" % conviva_metriclens_dimensions[0])


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_multi_metriclens_dimension(conviva_metriclens_dimensions):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: quality_metriclens
            metricLensDimensions: {conviva_metriclens_dimensions}
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        for dim in conviva_metriclens_dimensions:
            if dim != "CDNs":
                assert wait_for(p(has_datapoint_with_dim_key, agent.fake_services, get_dim_key(dim)), 60), (
                    "Didn't get conviva datapoints with %s dimension" % dim
                )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_all_metriclens_dimension(conviva_metriclens_dimensions):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: quality_metriclens
            metricLensDimensions:
            - _ALL_
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        for dim in conviva_metriclens_dimensions:
            if dim != "CDNs":
                assert wait_for(p(has_datapoint_with_dim_key, agent.fake_services, get_dim_key(dim)), 60), (
                    "Didn't get conviva datapoints with %s dimension" % dim
                )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_exclude_metriclens_dimension():
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: quality_metriclens
            metricLensDimensions:
            - _ALL_
            excludeMetricLensDimensions:
            - CDNs
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        assert ensure_always(
            lambda: not has_datapoint_with_dim_key(agent.fake_services, "CDNs")
        ), "Received datapoint with excluded CDNs dimension"


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_metric_account(conviva_accounts):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
            account: {conviva_accounts[0]}
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        assert ensure_always(
            p(
                all_datapoints_have_metric_name_and_dims,
                agent.fake_services,
                "conviva.concurrent_plays",
                {"account": conviva_accounts[0]},
            )
        ), (
            "Received conviva datapoints without metric conviva.concurrent_plays or {account: %s} dimension"
            % conviva_accounts[0]
        )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_single_filter(conviva_filters):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
            filters:
              - {conviva_filters[0]}
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(lambda: agent.fake_services.datapoints), "Didn't get conviva datapoints"
        assert ensure_always(
            p(
                all_datapoints_have_metric_name_and_dims,
                agent.fake_services,
                "conviva.concurrent_plays",
                {"filter": conviva_filters[0]},
            )
        ), (
            "Received conviva datapoints without metric conviva.concurrent_plays or {filter: %s} dimension"
            % conviva_filters[0]
        )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_multi_filter(conviva_filters):
    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
            filters: {conviva_filters[:3]}
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        for filt in conviva_filters[:3]:
            assert wait_for(p(has_datapoint, agent.fake_services, "conviva.concurrent_plays", {"filter": filt}), 60), (
                "Didn't get conviva datapoints for metric concurrent_plays with dimension {filter: %s}" % filt
            )


@pytest.mark.flaky(reruns=2, reruns_delay=30)
def test_conviva_all_filter(conviva_filters):
    def get_filters_from_datapoints(agent):
        filters = set()
        for dp in agent.fake_services.datapoints:
            for dim in dp.dimensions:
                if dim.key == "filter":
                    filters.add(dim.value)
        return sorted(list(filters))

    with Agent.run(
        dedent(
            f"""
        intervalSeconds: 5
        monitors:
        - type: conviva
          pulseUsername: {{"#from": "env:CONVIVA_PULSE_USERNAME"}}
          pulsePassword: {{"#from": "env:CONVIVA_PULSE_PASSWORD"}}
          metricConfigs:
          - metricParameter: concurrent_plays
            maxFiltersPerRequest: 99
            filters:
              - _ALL_
    """
        ),
        debug=CONVIVA_DEBUG,
    ) as agent:
        assert wait_for(
            lambda: sorted(conviva_filters) == get_filters_from_datapoints(agent), 300
        ), "Didn't get datapoints with all filters"
