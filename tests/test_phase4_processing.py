"""Synthetic internal-contract fixtures; real signatures are tested separately."""
from datetime import datetime, timedelta, timezone
import math
import random
import sys

import pytest
from pydantic import ValidationError

from core.contracts import FeatureWindow, Location, ReferenceObservation, SignedReading, VerifiedReading
from core.processing.alignment import window_bounds
from core.processing.engine import ProcessingEngine
from core.processing.features import stable_mean, stable_std, stable_median
from core.processing.policy import ProcessingPolicy

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = START + timedelta(seconds=60)
HERE = Location(latitude=36.8, longitude=10.1)
NEAR = Location(latitude=36.801, longitude=10.1)


def event(value=20.0, *, sensor='sensor_01', sequence=0, seconds=0, unit='C',
          status='valid', checked=None, location=None, stream='stream_01', quantity='temperature'):
    raw = SignedReading(schema_version='1.0',sensor_id=sensor,key_id='test_key',stream_id=stream,
        timestamp=(START+timedelta(seconds=seconds)).strftime('%Y-%m-%dT%H:%M:%S.%fZ'),
        sequence=sequence,quantity=quantity,value=value,unit=unit,location=location,
        prev_hash=bytes(32),signature=bytes(64))
    sig = False if status=='bad_signature' else None if status in ('unknown_sensor','revoked_key') else True
    return VerifiedReading(schema_version='1.0',reading=raw,status=status,signature_verified=sig,
                           checked_at=checked or END)


def reference(value=20.0, *, observed=0, available=0, source='official_test_fixture', location=HERE, unit='C'):
    return ReferenceObservation(source_id=source,observed_at=START+timedelta(seconds=observed),
        available_at=START+timedelta(seconds=available),quantity='temperature',value=value,unit=unit,location=location)


def build(events, *, policy=None, refs=(), as_of=END):
    return ProcessingEngine(policy).build_window(events,start=START,end=END,as_of=as_of,references=refs)


def values(window, group='temporal_features', sensor='sensor_01'):
    return {v.name:v.value for v in getattr(window,group).values if v.sensor_id==sensor}


def test_known_temporal_statistics():
    window = build([event(v,sequence=i,seconds=i*2) for i,v in enumerate((20.0,22.0,24.0))]).window
    f=values(window)
    assert f['mean_c']==22.0
    assert f['std_c']==pytest.approx(math.sqrt(8/3))
    assert f['last_delta_c']==2.0 and f['last_dt_seconds']==2.0 and f['last_rate_c_per_s']==1.0
    assert f['rolling_mean_c']==22.0 and f['constant_run']==0.0
    assert f['dominant_frequency_hz'] is None


def test_rolling_tail_and_constant_signal():
    events=[event(v,sequence=i,seconds=i) for i,v in enumerate((2.0,4.0,6.0,6.0,6.0))]
    f=values(build(events,policy=ProcessingPolicy(rolling_samples=3)).window)
    assert f['rolling_mean_c']==6.0 and f['rolling_std_c']==0.0 and f['constant_run']==1.0
    assert f['mean_c']==pytest.approx(4.8)


def test_signed_null_breaks_temporal_segment():
    events=[event(20.0,sequence=0),event(None,sequence=1,seconds=1),event(22.0,sequence=2,seconds=2)]
    result=build(events)
    f=values(result.window)
    assert f['sample_count']==3 and f['usable_count']==2 and f['missing_count']==1
    assert f['rolling_count']==1 and f['last_delta_c'] is None
    assert result.window.readings[1].provenance.reading.value is None


def test_last_invalid_sample_does_not_use_an_older_rolling_value():
    f=values(build([event(),event(None,sequence=1,seconds=1)]).window)
    assert f['rolling_count']==0 and f['rolling_mean_c'] is None


@pytest.mark.parametrize('kind', ['gap','same_time','new_stream','bad_signature'])
def test_discontinuity_disables_pairwise_and_spectral(kind):
    first=event()
    second=event(21.0,sequence=2 if kind=='gap' else 1,seconds=0 if kind=='same_time' else 1,
                 stream='new' if kind=='new_stream' else 'stream_01',status='bad_signature' if kind=='bad_signature' else 'valid')
    f=values(build([first,second]).window)
    assert f['last_delta_c'] is None and f['spectral_eligible']==0


def test_sinusoid_spectral_frequency():
    events=[event(20.0+2*math.sin(2*math.pi*i/8),sequence=i,seconds=i) for i in range(32)]
    f=values(build(events).window)
    assert f['spectral_eligible']==1
    assert f['dominant_frequency_hz']==pytest.approx(0.125)
    assert f['dominant_power_fraction']==pytest.approx(1.0)


def test_constant_has_no_fabricated_dominant_frequency():
    f=values(build([event(20.0,sequence=i,seconds=i) for i in range(8)]).window)
    assert f['spectral_eligible']==1 and f['dominant_frequency_hz'] is None
    assert f['dominant_power_fraction'] is None and f['std_c']==0.0


def test_irregular_sampling_disables_spectral():
    times=(0,1,2,4,5,6,7,8)
    f=values(build([event(float(i),sequence=i,seconds=t) for i,t in enumerate(times)]).window)
    assert f['spectral_eligible']==0 and f['dominant_frequency_hz'] is None


def test_huge_values_do_not_crash_or_emit_nan_inf():
    large=sys.float_info.max
    f=values(build([event(large,sequence=i,seconds=i) for i in range(8)]).window)
    assert f['mean_c']==large and f['std_c']==0.0
    assert all(v is None or math.isfinite(v) for v in f.values())
    assert stable_median([large,large])==large
    assert stable_mean([large,large])==large
    assert stable_std([large,large])==0.0


def test_overflowing_rate_becomes_null():
    f=values(build([event(0.0),event(sys.float_info.max,sequence=1,seconds=0.000001)]).window)
    assert f['last_rate_c_per_s'] is None


def test_half_open_window_and_availability_cutoff():
    events=[event(sequence=0,seconds=-1),event(sequence=1,seconds=0),
            event(sequence=2,seconds=59.999999),event(sequence=3,seconds=60),
            event(sequence=4,seconds=30,checked=END+timedelta(microseconds=1))]
    result=build(events)
    assert [p.provenance.reading.sequence for p in result.window.readings]==[1,2]
    assert [x.reason for x in result.excluded_events].count('outside_window')==2
    assert any(x.reason=='not_available_as_of' for x in result.excluded_events)


def test_duplicate_replay_does_not_replace_valid_or_double_count():
    original=event()
    replay=event(status='replay')
    result=build([replay,original,original])
    assert len(result.window.readings)==1 and result.window.readings[0].ready_for_analysis
    assert len(result.excluded_events)==2 and values(result.window)['sample_count']==1


def test_rejected_forgery_does_not_poison_authentic_reading():
    result=build([event(99.0,status='bad_signature'),event(20.0)])
    assert result.window.readings[0].normalized_value==20.0
    assert result.excluded_events[0].reason=='conflicting_identity'


def test_conflicting_valid_identity_is_quarantined_with_both_raw_values():
    result=build([event(20.0),event(99.0)])
    assert result.window.readings[0].normalized_value is None
    assert 'conflicting_verified_identity' in result.window.readings[0].quality_flags
    assert {result.window.readings[0].provenance.reading.value,result.excluded_events[0].event.reading.value}=={20.0,99.0}


def test_malformed_evidence_cannot_enter_window():
    malformed=VerifiedReading(schema_version='1.0',reading=None,status='malformed',signature_verified=None,
                              checked_at=END,raw_message_sha256=bytes(32))
    result=build([malformed])
    assert not result.window.readings
    assert result.excluded_events[0].reason=='malformed_no_event_time'
    assert result.window.temporal_features.availability=='unavailable'


def test_schema_roundtrip_and_input_order_invariance():
    events=[event(float(i),sequence=i,seconds=i) for i in range(8)]
    first=build(events).window
    random.Random(42).shuffle(events)
    second=build(events).window
    assert first==second
    assert FeatureWindow.model_validate_json(first.model_dump_json())==first


def test_policy_changes_window_id():
    events=[event()]
    assert build(events).window.window_id != build(events,policy=ProcessingPolicy(rolling_samples=3)).window.window_id


def test_window_bounds_timezone_and_before_epoch():
    shifted=START.astimezone(timezone(timedelta(hours=1)))
    assert window_bounds(shifted)==(START,END)
    old=datetime(1969,12,31,23,59,59,tzinfo=timezone.utc)
    assert window_bounds(old)[0].second==0
    with pytest.raises(ValueError): window_bounds(datetime(2026,1,1))


def test_partition_mixed_sampling_and_open_window_evidence():
    events=[event(sequence=0), event(sequence=1,seconds=59), event(sequence=2,seconds=60),
            event(sensor='sensor_02',seconds=30)]
    results,excluded=ProcessingEngine().build_closed_windows(events,as_of=END)
    assert len(results)==1 and len(results[0].window.readings)==3
    assert excluded[0].reason=='window_not_closed'
    results,_=ProcessingEngine().build_closed_windows(events,as_of=END+timedelta(seconds=60))
    assert len(results)==2 and sum(len(r.window.readings) for r in results)==4


def test_input_bounds_and_invalid_windows():
    with pytest.raises(BufferError): build([event(),event(sequence=1)],policy=ProcessingPolicy(max_events=1))
    with pytest.raises(BufferError): build([event(),event(sensor='s2')],policy=ProcessingPolicy(max_sensors=1))
    with pytest.raises(ValueError): build([],as_of=START)
    with pytest.raises(ValueError):
        ProcessingEngine().build_window([],start=START+timedelta(seconds=1),end=END,as_of=END)
    with pytest.raises(TypeError): build([event().model_dump()])


@pytest.mark.parametrize('kwargs',[{'window_seconds':0},{'window_seconds':True},{'constant_tolerance_c':float('nan')},
    {'neighbor_pairs':(('a','a'),)},{'reference_sources':('a','a')},{'max_sensors':101}])
def test_invalid_policy(kwargs):
    with pytest.raises(ValidationError): ProcessingPolicy(**kwargs)


def test_missing_spatial_and_reference_evidence_is_explicit():
    result=build([event()]).window
    assert result.spatial_features.reason=='neighbors_not_configured'
    assert result.reference_features.reason=='reference_sources_not_configured'


def test_neighbor_consensus_one_vote_per_sensor_and_no_future_neighbor():
    policy=ProcessingPolicy(neighbor_pairs=(('sensor_01','sensor_02'),('sensor_01','sensor_03')))
    events=[event(30.0,seconds=10,location=HERE),event(20.0,sensor='sensor_02',location=NEAR),
        event(22.0,sensor='sensor_02',sequence=1,seconds=9,location=NEAR),
        event(99.0,sensor='sensor_02',sequence=2,seconds=11,location=NEAR),
        event(24.0,sensor='sensor_03',seconds=10,location=NEAR)]
    f=values(build(events,policy=policy).window,'spatial_features')
    assert f['neighbor_count']==2 and f['neighbor_median_c']==23.0 and f['neighbor_deviation_c']==7.0
    assert f['neighbor_std_c']==pytest.approx(1.0)


@pytest.mark.parametrize('kind',['missing_location','too_far','too_old','bad_provenance','unknown_unit','unconfigured'])
def test_ineligible_neighbor_not_used(kind):
    policy=ProcessingPolicy(neighbor_pairs=(('sensor_01','sensor_02'),),spatial_max_age_seconds=2.0)
    peer=event(sensor='sensor_03' if kind=='unconfigured' else 'sensor_02',
        location=None if kind=='missing_location' else Location(latitude=0.0,longitude=0.0) if kind=='too_far' else NEAR,
        seconds=0 if kind=='too_old' else 10,status='bad_signature' if kind=='bad_provenance' else 'valid',
        unit='unknown' if kind=='unknown_unit' else 'C')
    window=build([event(seconds=10,location=HERE),peer],policy=policy).window
    assert window.spatial_features.availability=='unavailable'


def test_causal_reference_conversion_and_raw_retention():
    policy=ProcessingPolicy(reference_sources=('official_test_fixture',))
    ref=reference(68.0,unit='F',observed=5,available=20)
    result=build([event(25.0,seconds=10,location=HERE)],policy=policy,refs=(ref,))
    f=values(result.window,'reference_features')
    assert f['reference_c']==20.0 and f['reference_deviation_c']==5.0 and f['reference_age_seconds']==5.0
    assert result.window.reference_data==(ref,)
    assert result.reference_matches[0].sensor_id=='sensor_01'
    assert result.reference_matches[0].reference==ref


@pytest.mark.parametrize('kind',['future_event','late_available','too_old','too_far','missing_location','unknown_source','unknown_unit'])
def test_reference_no_leakage_or_untrusted_match(kind):
    policy=ProcessingPolicy(reference_sources=('official_test_fixture',),reference_max_age_seconds=5.0)
    ref=reference(observed=11 if kind=='future_event' else 0 if kind=='too_old' else 10,
        available=61 if kind=='late_available' else 10,
        location=None if kind=='missing_location' else Location(latitude=0.0,longitude=0.0) if kind=='too_far' else HERE,
        source='other' if kind=='unknown_source' else 'official_test_fixture',unit='X' if kind=='unknown_unit' else 'C')
    result=build([event(seconds=10,location=HERE)],policy=policy,refs=(ref,))
    assert result.window.reference_features.availability=='unavailable'
    if kind=='late_available': assert result.excluded_references[0].reason=='not_available_as_of'


def test_ambiguous_reference_is_not_arbitrarily_selected():
    policy=ProcessingPolicy(reference_sources=('official_test_fixture',))
    result=build([event(seconds=10,location=HERE)],policy=policy,refs=(reference(20.0),reference(21.0)))
    assert result.window.reference_features.availability=='unavailable'


def test_latest_available_reference_revision_and_duplicate_evidence():
    policy=ProcessingPolicy(reference_sources=('official_test_fixture',))
    old,new=reference(20.0),reference(21.0,available=5)
    result=build([event(seconds=10,location=HERE)],policy=policy,refs=(old,new,new))
    assert values(result.window,'reference_features')['reference_c']==21.0
    assert len(result.window.reference_data)==2
    assert result.excluded_references[0].reason=='duplicate_reference'


def test_ref_bound_and_future_end_exclusion():
    with pytest.raises(BufferError): build([],refs=(reference(),reference()),policy=ProcessingPolicy(max_references=1))
    result=build([],refs=(reference(observed=60),))
    assert not result.window.reference_data
    assert result.excluded_references[0].reason=='reference_at_or_after_end'


def test_wrong_type_missing_timestamp_stays_at_contract_boundary():
    raw=event().reading.model_dump()
    del raw['timestamp']
    with pytest.raises(ValidationError): SignedReading(**raw)
    raw=event().reading.model_dump()
    raw['value']=True
    with pytest.raises(ValidationError): SignedReading(**raw)


def test_all_unusable_samples_have_counts_but_no_fabricated_statistics():
    result=build([event(None),event(20.0,sequence=1,seconds=1,unit='unknown')])
    f=values(result.window)
    assert f['usable_count']==0 and f['excluded_count']==2
    assert f['mean_c'] is None and f['std_c'] is None and f['min_c'] is None


def test_partition_has_a_bounded_number_of_windows():
    events=[event(sequence=i,seconds=60*i) for i in range(129)]
    with pytest.raises(BufferError):
        ProcessingEngine().build_closed_windows(events,as_of=START+timedelta(seconds=60*130))


def test_naive_cutoff_and_reference_dicts_rejected():
    with pytest.raises(ValueError): build([],as_of=datetime(2026,1,1))
    with pytest.raises(TypeError): build([],refs=(reference().model_dump(),))


def test_reference_and_spatial_numeric_extremes_are_finite():
    maximum=sys.float_info.max
    policy=ProcessingPolicy(neighbor_pairs=(('sensor_01','sensor_02'),('sensor_01','sensor_03')),
                            reference_sources=('official_test_fixture',))
    events=[event(maximum,location=HERE),event(maximum,sensor='sensor_02',location=NEAR),
            event(maximum,sensor='sensor_03',location=NEAR)]
    window=build(events,policy=policy,refs=(reference(maximum),)).window
    assert values(window,'spatial_features')['neighbor_median_c']==maximum
    assert values(window,'reference_features')['reference_c']==maximum
    assert FeatureWindow.model_validate_json(window.model_dump_json())==window
