"""Offline optimization must preserve wire evidence and replay semantics."""
from datetime import datetime, timezone
import gzip
import sqlite3
import pytest

from adapters.dataset import Measurement
from attacks.scenario import DatasetExperiment
from scripts.export_wegenernet_halfhourly import export
from scripts.validate_wegenernet_export import validate
from tests.test_halfhourly_integration import fixture


def test_export_matches_disk_including_warmup_and_resume(tmp_path):
    source = fixture(tmp_path/'source.nc',range(1,60))
    start,end='2024-01-01T00:00:00Z','2024-01-02T04:30:00Z'
    disk, fast = tmp_path/'disk',tmp_path/'fast'
    export([source],[(1,1)],disk,start,end,offline_memory=False)
    export([source],[(1,1)],fast,start,end)
    for path in disk.rglob('*.gz'):
        assert gzip.decompress(path.read_bytes()) == gzip.decompress((fast/path.relative_to(disk)).read_bytes())
    assert (disk/'experiment-public-keys.json').read_bytes()==(fast/'experiment-public-keys.json').read_bytes()
    assert validate(fast,[source])['rows']==57
    (fast/'2024-01'/'SUCCESS.json').unlink()
    export([source],[(1,1)],fast,start,end,resume=True)
    for path in disk.rglob('*.gz'):
        assert gzip.decompress(path.read_bytes()) == gzip.decompress((fast/path.relative_to(disk)).read_bytes())


def test_memory_verifier_retains_replay_checks_and_monotonic_ids(tmp_path):
    session=DatasetExperiment(tmp_path,['s01'],'test',offline_memory=True)
    sample=Measurement(sensor_id='s01',observed_at=datetime(2024,1,1,tzinfo=timezone.utc),value=280.,unit='K',source_id='fixture',source_record_id='synthetic')
    try:
        first,event,wire=session.verify(sample)
        assert event.status=='valid'
        session.verifier.acknowledge(first)
        session.discard_delivered()
        assert session.registry.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
        replay,rejected=session.verifier.verify(wire)
        assert replay>first and rejected.status=='replay'
        session.verifier.acknowledge(replay)
        session.discard_delivered()
        second,valid,_=session.verify(sample)
        assert second>replay and valid.status=='valid' and valid.reading.sequence==1
        assert not session.state.pending(session.signers['s01'].identity)
        with pytest.raises(RuntimeError):
            with session.registry.transaction() as db:
                db.execute('UPDATE heads SET next_sequence=999')
                raise RuntimeError('simulated failed transaction')
        assert session.registry.db.execute('SELECT next_sequence FROM heads').fetchone()[0]==2
        # Many purges must not grow the offline journal or reset IDs/chains.
        session.verifier.acknowledge(second)
        for _ in range(200):
            eid,event,_=session.verify(sample)
            assert event.status=='valid'
            session.verifier.acknowledge(eid)
            session.discard_delivered()
        assert session.registry.db.execute('SELECT COUNT(*) FROM events').fetchone()[0]==0
        assert session.registry.db.execute('PRAGMA page_count').fetchone()[0]<30
    finally:
        session.close()
    with pytest.raises(sqlite3.ProgrammingError):session.registry.db.execute('SELECT 1')
