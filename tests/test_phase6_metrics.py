import math
import pytest
from eval.metrics import LabeledDecision, binary_metrics, threshold_decisions
from eval.performance import summarize_ns, resources


def test_confusion_matrix_and_rates():
    decisions=[LabeledDecision(str(i),a,p) for i,(a,p) in enumerate([(True,True),(True,True),(True,False),(False,True),(False,False),(False,False)])]
    result=binary_metrics(decisions)
    assert (result['true_positive'],result['true_negative'],result['false_positive'],result['false_negative'])==(2,2,1,1)
    assert result['detection_rate']==pytest.approx(2/3)
    assert result['false_positive_rate']==pytest.approx(1/3)
    assert result['precision']==pytest.approx(2/3) and result['f1']==pytest.approx(2/3)


def test_empty_and_undefined_denominators():
    empty=binary_metrics([])
    assert empty['samples']==0 and empty['detection_rate'] is None and empty['accuracy'] is None
    clean=binary_metrics([LabeledDecision('a',False,False)])
    assert clean['false_positive_rate']==0 and clean['precision'] is None and clean['f1'] is None


@pytest.mark.parametrize('items', [[LabeledDecision('a',1,True)],[LabeledDecision('',True,False)],
    [LabeledDecision('a',True,False),LabeledDecision('a',False,True)]])
def test_invalid_decisions(items):
    with pytest.raises((TypeError,ValueError)): binary_metrics(items)


def test_threshold_alignment_and_bounds():
    predictions=threshold_decisions({'a':True,'b':False},{'b':0.2,'a':0.5},threshold=0.5)
    assert binary_metrics(predictions)['accuracy']==1
    with pytest.raises(ValueError): threshold_decisions({'a':True},{},threshold=.5)
    with pytest.raises(ValueError): threshold_decisions({'a':True},{'a':float('nan')},threshold=.5)
    with pytest.raises(BufferError): binary_metrics(predictions,maximum=1)


def test_nearest_rank_uses_actual_sample_count():
    result=summarize_ns([1_000_000,2_000_000,3_000_000,4_000_000])
    assert result['mean_ms']==2.5 and result['p50_ms']==2 and result['p95_ms']==4
    for invalid in ([],[-1],[True],[1.5]):
        with pytest.raises(ValueError): summarize_ns(invalid)


def test_resource_observations_are_nonnegative():
    result=resources(lambda:bytearray(2000),iterations=3)
    assert result['process_cpu_seconds']>=0 and result['python_traced_peak_bytes']>=2000
