import pathlib
import subprocess
from typing import Optional

from fixtures.neon_fixtures import (
    DEFAULT_BRANCH_NAME,
    NeonEnv,
    NeonEnvBuilder,
    NeonPageserverHttpClient,
    neon_binpath,
    pg_distrib_dir,
    wait_until,
)
from fixtures.types import Lsn, ZTenantId, ZTimelineId


# test that we cannot override node id after init
def test_pageserver_init_node_id(neon_simple_env: NeonEnv):
    repo_dir = neon_simple_env.repo_dir
    pageserver_config = repo_dir / "pageserver.toml"
    pageserver_bin = pathlib.Path(neon_binpath) / "pageserver"

    def run_pageserver(args):
        return subprocess.run(
            [str(pageserver_bin), "-D", str(repo_dir), *args],
            check=False,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    # remove initial config
    pageserver_config.unlink()

    bad_init = run_pageserver(["--init", "-c", f'pg_distrib_dir="{pg_distrib_dir}"'])
    assert (
        bad_init.returncode == 1
    ), "pageserver should not be able to init new config without the node id"
    assert "missing id" in bad_init.stderr
    assert not pageserver_config.exists(), "config file should not be created after init error"

    completed_init = run_pageserver(
        ["--init", "-c", "id = 12345", "-c", f'pg_distrib_dir="{pg_distrib_dir}"']
    )
    assert (
        completed_init.returncode == 0
    ), "pageserver should be able to create a new config with the node id given"
    assert pageserver_config.exists(), "config file should be created successfully"

    bad_reinit = run_pageserver(
        ["--init", "-c", "id = 12345", "-c", f'pg_distrib_dir="{pg_distrib_dir}"']
    )
    assert (
        bad_reinit.returncode == 1
    ), "pageserver should not be able to init new config without the node id"
    assert "already exists, cannot init it" in bad_reinit.stderr

    bad_update = run_pageserver(["--update-config", "-c", "id = 3"])
    assert bad_update.returncode == 1, "pageserver should not allow updating node id"
    assert "has node id already, it cannot be overridden" in bad_update.stderr


def check_client(client: NeonPageserverHttpClient, initial_tenant: ZTenantId):
    client.check_status()

    # check initial tenant is there
    assert initial_tenant in {ZTenantId(t["id"]) for t in client.tenant_list()}

    # create new tenant and check it is also there
    tenant_id = ZTenantId.generate()
    client.tenant_create(tenant_id)
    assert tenant_id in {ZTenantId(t["id"]) for t in client.tenant_list()}

    timelines = client.timeline_list(tenant_id)
    assert len(timelines) == 0, "initial tenant should not have any timelines"

    # create timeline
    timeline_id = ZTimelineId.generate()
    client.timeline_create(tenant_id=tenant_id, new_timeline_id=timeline_id)

    timelines = client.timeline_list(tenant_id)
    assert len(timelines) > 0

    # check it is there
    assert timeline_id in {ZTimelineId(b["timeline_id"]) for b in client.timeline_list(tenant_id)}
    for timeline in timelines:
        timeline_id = ZTimelineId(timeline["timeline_id"])
        timeline_details = client.timeline_detail(
            tenant_id=tenant_id,
            timeline_id=timeline_id,
            include_non_incremental_logical_size=True,
        )

        assert ZTenantId(timeline_details["tenant_id"]) == tenant_id
        assert ZTimelineId(timeline_details["timeline_id"]) == timeline_id

        local_timeline_details = timeline_details.get("local")
        assert local_timeline_details is not None
        assert local_timeline_details["timeline_state"] == "Loaded"


def test_pageserver_http_get_wal_receiver_not_found(neon_simple_env: NeonEnv):
    env = neon_simple_env
    with env.pageserver.http_client() as client:
        tenant_id, timeline_id = env.neon_cli.create_tenant()

        timeline_details = client.timeline_detail(
            tenant_id=tenant_id, timeline_id=timeline_id, include_non_incremental_logical_size=True
        )

        assert (
            timeline_details.get("wal_source_connstr") is None
        ), "Should not be able to connect to WAL streaming without PG compute node running"
        assert (
            timeline_details.get("last_received_msg_lsn") is None
        ), "Should not be able to connect to WAL streaming without PG compute node running"
        assert (
            timeline_details.get("last_received_msg_ts") is None
        ), "Should not be able to connect to WAL streaming without PG compute node running"


def expect_updated_msg_lsn(
    client: NeonPageserverHttpClient,
    tenant_id: ZTenantId,
    timeline_id: ZTimelineId,
    prev_msg_lsn: Optional[Lsn],
) -> Lsn:
    timeline_details = client.timeline_detail(tenant_id, timeline_id=timeline_id)

    # a successful `timeline_details` response must contain the below fields
    local_timeline_details = timeline_details["local"]
    assert "wal_source_connstr" in local_timeline_details.keys()
    assert "last_received_msg_lsn" in local_timeline_details.keys()
    assert "last_received_msg_ts" in local_timeline_details.keys()

    assert (
        local_timeline_details["last_received_msg_lsn"] is not None
    ), "the last received message's LSN is empty"

    last_msg_lsn = Lsn(local_timeline_details["last_received_msg_lsn"])
    assert (
        prev_msg_lsn is None or prev_msg_lsn < last_msg_lsn
    ), f"the last received message's LSN {last_msg_lsn} hasn't been updated \
        compared to the previous message's LSN {prev_msg_lsn}"

    return last_msg_lsn


# Test the WAL-receiver related fields in the response to `timeline_details` API call
#
# These fields used to be returned by a separate API call, but they're part of
# `timeline_details` now.
def test_pageserver_http_get_wal_receiver_success(neon_simple_env: NeonEnv):
    env = neon_simple_env
    with env.pageserver.http_client() as client:
        tenant_id, timeline_id = env.neon_cli.create_tenant()
        pg = env.postgres.create_start(DEFAULT_BRANCH_NAME, tenant_id=tenant_id)

        # Wait to make sure that we get a latest WAL receiver data.
        # We need to wait here because it's possible that we don't have access to
        # the latest WAL yet, when the `timeline_detail` API is first called.
        # See: https://github.com/neondatabase/neon/issues/1768.
        lsn = wait_until(
            number_of_iterations=5,
            interval=1,
            func=lambda: expect_updated_msg_lsn(client, tenant_id, timeline_id, None),
        )

        # Make a DB modification then expect getting a new WAL receiver's data.
        pg.safe_psql("CREATE TABLE t(key int primary key, value text)")
        wait_until(
            number_of_iterations=5,
            interval=1,
            func=lambda: expect_updated_msg_lsn(client, tenant_id, timeline_id, lsn),
        )


def test_pageserver_http_api_client(neon_simple_env: NeonEnv):
    env = neon_simple_env
    with env.pageserver.http_client() as client:
        check_client(client, env.initial_tenant)


def test_pageserver_http_api_client_auth_enabled(neon_env_builder: NeonEnvBuilder):
    neon_env_builder.auth_enabled = True
    env = neon_env_builder.init_start()

    management_token = env.auth_keys.generate_management_token()

    with env.pageserver.http_client(auth_token=management_token) as client:
        check_client(client, env.initial_tenant)