import numpy as np

from aerodoppler.physics import nearest_approach, retarded_from_positions, straight_doppler


def test_closed_form_matches_retarded_straight_line():
    speed, slant, f0, c = 120.0, 400.0, 180.0, 343.0
    tau = np.linspace(-6.0, 6.0, 161)
    position = np.column_stack((speed * tau, np.zeros_like(tau), np.zeros_like(tau)))
    observer = np.array([0.0, slant, 0.0])
    samples = retarded_from_positions(tau, position, observer, f0, c)
    predicted = straight_doppler(samples["receive_s"], f0, speed, slant, slant / c, c)
    interior = slice(8, -8)
    assert np.allclose(samples["frequency_hz"][interior], predicted[interior], rtol=2e-3, atol=0.3)
    approach = nearest_approach(samples)
    assert approach["distance_m"] == min(samples["distance_m"])
    cpa = int(np.argmin(np.abs(tau)))
    assert samples["receive_s"][cpa] == samples["emission_s"][cpa] + samples["distance_m"][cpa] / c


def test_receding_source_lowers_frequency():
    frequency = straight_doppler(np.array([10.0]), 200.0, 80.0, 500.0, 0.0)
    assert float(frequency[0]) < 200.0
