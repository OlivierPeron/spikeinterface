"""Sorting components: peak waveform features."""
import numpy as np

from spikeinterface.core.job_tools import ChunkRecordingExecutor, _shared_job_kwargs_doc
from spikeinterface.core import get_chunk_with_margin, get_channel_distances


one_feature_per_peak_params = {"amplitude" : {"peak_sign" : "neg", "ms_before" : None, "ms_after" : None},
                         "ptp" : {"ms_before" : None, "ms_after" : None},
                         "com" : {"local_radius_um" : 50, "ms_before" : None, "ms_after" : None},
                         "dist_com_vs_max_ptp_channel" : {"local_radius_um" : 50, "ms_before" : None, "ms_after" : None},
                         "energy" : {"local_radius_um" : 50, "ms_before" : None, "ms_after" : None}
                        }

n_chans_features_per_peak_params = {"amplitude" : {"peak_sign" : "neg", "ms_before" : None, "ms_after" : None},
                             "ptp" : {"ms_before" : None, "ms_after" : None}}


def compute_features_from_peaks(
    recording,
    peaks,
    feature_list=["amplitude", "ptp"],
    feature_params = {},
    ms_before=1, 
    ms_after=1,
    one_feature_per_peak=True,
    smoothing=None,
    **job_kwargs,
):
    """Extract features on the fly from the recording given a list of peaks. 

    Parameters
    ----------
    recording: RecordingExtractor
        The recording extractor object.
    peaks: array
        Peaks array, as returned by detect_peaks() in "compact_numpy" way.
    feature_list: List of features to be computed.
        If one_feature_per_peak is True, can be chosen between
            - amplitude (params: ms_before, ms_after, peak_sign)
            - ptp (params: ms_before, ms_after)
            - com (params: ms_before, ms_after, local_radius_um)
            - dist_com_vs_max_p2p_channel (params: ms_before, ms_after, local_radius_um)
            - energy (params: ms_before, ms_after, local_radius_um)
        If one_feature_per_peak is False, can be chosen between
            - amplitude (params: ms_before, ms_after, peak_sign)
            - ptp (params: ms_before, ms_after)
        Note that if all features have common ms_before, ms_after, this is faster not to
        specify them in these individual dicts
    one_feature_per_peak: bool
        If True, we only get one single feature per peak. If False, then values over all
        channels are returned.
    ms_before: float
        The duration in ms before the peak for extracting the features (default 1 ms)
    ms_after: float
        The duration in ms  after the peakfor extracting the features (default 1 ms)
    smoothing: can be None, or 'savgol'

    {}

    Returns
    -------
    waveform_features: ndarray (NxCxF) if one_feature_per_peak is False, or (NxF) if True
        Array with waveform features for each spike.
    """

    if one_feature_per_peak_params:
        my_params = one_feature_per_peak_params.copy()
    else:
        my_params = n_chans_features_per_peak_params.copy()

    for key in feature_params.keys():
        my_params[key].update(feature_params[key])

    has_com = False
    for feature in feature_list:
        assert feature in my_params.keys(), "feature is not known..."
        if feature == 'com':
            has_com = True
        if feature in ['dist_com_vs_max_ptp_channel']:
            assert has_com, "some features requires CoM to be computed first"

    nbefore_max = 0
    nafter_max = 0

    my_params["global_times"] = True

    for feature in feature_list:
        if my_params[feature]["ms_before"] is not None:
            ms_before = my_params[feature]['ms_before']
            my_params["global_times"] = False

        nbefore = int(ms_before * recording.get_sampling_frequency() / 1000.0)
        my_params[feature]['nbefore'] = nbefore

        if my_params[feature]["ms_after"] is not None:
            ms_after = my_params[feature]['ms_after']
            my_params["global_times"] = False

        nafter = int(ms_after * recording.get_sampling_frequency() / 1000.0)
        my_params[feature]['nafter'] = nafter

        if nbefore > nbefore_max:
            nbefore_max = nbefore

        if nafter > nafter_max:
            nafter_max = nafter

        if "local_radius_um" in my_params[feature]:
            num_chans = recording.get_num_channels()
            sparsity_mask = np.zeros((peaks.size, num_chans), dtype='bool')
            chan_locs = recording.get_channel_locations()
            unit_inds = range(num_chans)
            chan_distances = get_channel_distances(recording)

            for main_chan in unit_inds:
                closest_chans, = np.nonzero(chan_distances[main_chan, :] <= my_params[feature]['local_radius_um'])
                sparsity_mask[main_chan, closest_chans] = True
            my_params[feature]['sparsity_mask'] = sparsity_mask
            my_params['chan_locs'] = chan_locs

        if feature == "com":
            my_params['nb_com_dims'] = len(recording.get_channel_locations().shape)


    my_params['nbefore'] = nbefore_max
    my_params['nafter'] = nafter_max
    my_params['smoothing'] = smoothing
    my_params['one_feature_per_peak'] = one_feature_per_peak

    # margin at border for get_trace
    margin = max(nbefore_max, nafter_max)

    # and run
    func = _compute_features_from_peaks_chunk  # _localize_peaks_chunk
    init_func = _init_worker_compute_features_from_peaks
    init_args = (
        recording.to_dict(),
        peaks,
        margin,
        feature_list,
        my_params,
    )
    processor = ChunkRecordingExecutor(
        recording,
        func,
        init_func,
        init_args,
        handle_returns=True,
        job_name="compute waveform features",
        **job_kwargs,
    )
    peak_waveform_features = processor.run()
    peak_waveform_features = np.concatenate(peak_waveform_features)

    return peak_waveform_features


compute_features_from_peaks.__doc__ = (
    compute_features_from_peaks.__doc__.format(_shared_job_kwargs_doc)
)


def _init_worker_compute_features_from_peaks(
    recording,
    peaks,
    margin,
    feature_list,
    feature_params
):
    """Initialize worker for localizing peaks."""

    if isinstance(recording, dict):
        from spikeinterface.core import load_extractor

        recording = load_extractor(recording)

    # create a local dict per worker
    worker_ctx = {}
    worker_ctx["recording"] = recording
    worker_ctx["peaks"] = peaks
    worker_ctx["margin"] = margin
    worker_ctx["feature_list"] = feature_list
    worker_ctx["feature_params"] = feature_params
    return worker_ctx


def _compute_features_from_peaks_chunk(segment_index, start_frame, end_frame, worker_ctx):
    """Localize peaks in a chunk of data."""

    # recover variables of the worker
    recording = worker_ctx["recording"]
    peaks = worker_ctx["peaks"]
    margin = worker_ctx["margin"]
    feature_list = worker_ctx["feature_list"]
    feature_params = worker_ctx["feature_params"]

    # load trace in memory
    recording_segment = recording._recording_segments[segment_index]
    traces, left_margin, right_margin = get_chunk_with_margin(
        recording_segment, start_frame, end_frame, None, margin, add_zeros=True
    )

    # get local peaks (sgment + start_frame/end_frame)
    i0 = np.searchsorted(peaks["segment_ind"], segment_index)
    i1 = np.searchsorted(peaks["segment_ind"], segment_index + 1)
    peak_in_segment = peaks[i0:i1]
    i0 = np.searchsorted(peak_in_segment["sample_ind"], start_frame)
    i1 = np.searchsorted(peak_in_segment["sample_ind"], end_frame)
    local_peaks = peak_in_segment[i0:i1]

    # make sample index local to traces
    local_peaks = local_peaks.copy()
    local_peaks["sample_ind"] -= start_frame - left_margin

    peak_waveform_features = compute_features(
        traces,
        local_peaks,
        feature_list,
        feature_params
    )

    return peak_waveform_features


def compute_features(
    traces, local_peak, feature_list, feature_params
):
    """Localize peaks using the center of mass method."""

    if feature_params['one_feature_per_peak']:
        if 'com' in feature_list:
            feature_size = len(feature_list) + (feature_params['nb_com_dims'] - 1)
            com_start = feature_list.index('com')
        else:
            feature_size = len(feature_list)

        peak_waveform_features = np.zeros(
            (local_peak.size, feature_size), dtype=np.float32
        )

    else:
        feature_size = len(feature_list)

        peak_waveform_features = np.zeros(
            (local_peak.size, traces.shape[1], feature_size), dtype=np.float32
        )

    feature_idx = 0

    _loop_idx = {}
    wf = None

    for feature in feature_list:

        if feature_params['global_times'] and wf is None:
            nbefore = feature_params['nbefore']
            nafter = feature_params['nafter']
            wf = traces[local_peak['sample_ind'][:, None] + np.arange(-nbefore, nafter), :]
        else:
            nbefore = feature_params[feature]['nbefore']
            nafter = feature_params[feature]['nafter']
            wf = traces[local_peak['sample_ind'][:, None] + np.arange(-nbefore, nafter), :]

        if feature == 'amplitude':
            if wf.shape[1] == 0:
                features = local_peak['amplitude']
            else:

                if feature_params['one_feature_per_peak']:
                    if feature_params[feature]['peak_sign'] == 'neg':
                        features = np.min(wf, axis=(1, 2))
                    elif feature_params[feature]['peak_sign'] == 'pos':
                        features = np.max(wf, axis=(1, 2))
                    elif feature_params[feature]['peak_sign'] == 'both':
                        features = np.max(np.abs(wf), axis=(1, 2))
                else:
                    if feature_params[feature]['peak_sign'] == 'both':
                        features = np.max(np.abs(wf, axis=1))
                    elif feature_params[feature]['peak_sign'] == 'neg':
                        features = np.min(wf, axis=1)
                    elif feature_params[feature]['peak_sign'] == 'pos':
                        features = np.max(wf, axis=1)

        elif feature == 'ptp':
            if feature_params['one_feature_per_peak']:
                all_ptps = np.ptp(wf, axis=1)
                features = np.max(all_ptps, axis=1)
            else:
                features = np.ptp(wf, axis=1)

        elif feature == 'energy':
            features = np.zeros(local_peak.size, dtype=np.float32)
            for main_chan in range(traces.shape[1]):
                if main_chan in _loop_idx:
                    idx =_loop_idx[main_chan]
                else:
                    idx = np.where(local_peak['channel_ind'] == main_chan)[0]
                    _loop_idx[main_chan] = idx
                nb_channels = np.sum(feature_params[feature]['sparsity_mask'][main_chan])
                features[idx] = np.linalg.norm(wf[idx] * feature_params[feature]['sparsity_mask'][main_chan], axis=(1, 2))/np.sqrt(nb_channels)

        elif feature == 'com':
            features = np.zeros((local_peak.size, feature_params['nb_com_dims']), dtype=np.float32)
            for main_chan in range(traces.shape[1]):
                if main_chan in _loop_idx:
                    idx =_loop_idx[main_chan]
                else:
                    idx = np.where(local_peak['channel_ind'] == main_chan)[0]
                    _loop_idx[main_chan] = idx
                chan_inds, = np.nonzero(feature_params[feature]['sparsity_mask'][main_chan])
                local_contact_locations = feature_params['chan_locs'][chan_inds, :]

                wf_ptp = (wf[idx][:, :, chan_inds]).ptp(axis=1)
                features[idx] = np.dot(wf_ptp, local_contact_locations)/(np.sum(wf_ptp, axis=1)[:,np.newaxis])

        elif feature == 'dist_com_vs_max_ptp_channel':
            features = np.zeros(local_peak.size, dtype=np.float32)
            for main_chan in range(traces.shape[1]):
                if main_chan in _loop_idx:
                    idx =_loop_idx[main_chan]
                else:
                    idx = np.where(local_peak['channel_ind'] == main_chan)[0]
                    _loop_idx[main_chan] = idx
                chan_inds, = np.nonzero(feature_params[feature]['sparsity_mask'][main_chan])
                local_contact_locations = feature_params['chan_locs'][chan_inds, :]

                wf_ptp = (wf[idx][:, :, chan_inds]).ptp(axis=1)
                max_ptp_channels = np.argmax(wf_ptp, axis=1)
                coms = peak_waveform_features[idx, com_start:com_start+feature_params['nb_com_dims']]
                features[idx] = np.linalg.norm(local_contact_locations[max_ptp_channels] - coms, axis=1)


        if feature_params['one_feature_per_peak']:
            if feature != 'com':
                peak_waveform_features[:, feature_idx] = features
                feature_idx += 1
            else:
                peak_waveform_features[:, feature_idx:feature_idx+feature_params['nb_com_dims']] = features
                feature_idx += feature_params['nb_com_dims']

        else:
            peak_waveform_features[:, :, feature_idx] = features
            feature_idx += 1

    return peak_waveform_features