# File:             BehavPreprocess.py
# Date:             Autumn 2021
# Description:      Reads .csv documents provided by the Muse headband set. (Describe these features)
#                   The preprocessing module of this pipline performs: 1) error filtering; 2) time-frequency analysis;
#                   3) frame normalization (optional);
# Authors:          Joao Campagnolo
# Python version:   Python 3.7+

# Import packages
import glob
import os
import numpy as np
import torch
from torch.utils.data import Dataset
from skimage import filters
import statistics
import time
import pandas as pd
from scipy.signal import savgol_filter

import headband_setup
from behav_annotation import Behav, label_gt_list
from preprocess import filter_batch
from find_wavelets import find_wav
from util import get_time

parietal_mask = [headband_setup.is_parietal_side(i) for i in range(headband_setup.num_electrodes)]
frontal_mask = [headband_setup.is_frontal_side(i) for i in range(headband_setup.num_electrodes)]
right_mask = [headband_setup.is_right_side(i) for i in range(headband_setup.num_electrodes)]
left_mask = [headband_setup.is_left_side(i) for i in range(headband_setup.num_electrodes)]

INTERVAL = 0
LABEL = 1


class BehavPreprocess(Dataset):
    def __init__(self, path_list, ti=0, tf=None, samp_rate=220, min_frequency=1, max_frequency=50, num_channels=30, bound_len=50, clip_len=100, smooth=True, mean_center=True, normalize=True, no_bounds=False, dump_rest=False):

        self.exp_path_list = path_list
        self.smooth = smooth
        self.mean_center = mean_center
        self.normalize = normalize
        self.no_bounds = no_bounds
        self.dump_rest = dump_rest
        self.min_frequency = min_frequency
        self.max_frequency = max_frequency
        self.samp_rate = samp_rate
        self.num_channels = num_channels
        self.clip_len = clip_len

        self.exp_eeg_list = list()
        self.smooth_exp_list = list()
        self.wav_exp_list = list()
        self.data_concat = list()
        self.frame_var_list = list()
        self.frame_amp_list = list()
        
        self.electrodes = headband_setup.electrode_names()
        
        self.time_start = time.time()
        
        # Reading data
        for exp in self.exp_path_list:
            exp_dict = pd.read_csv(exp)
            if tf is None:
                tf = int(exp_dict.size/len(exp_dict.keys()))
            else:
                tf = min(tf,int(exp_dict.size/len(exp_dict.keys())))
            assert tf > ti, 'tf must be greater that ti'
            self.exp_eeg_list.append(np.zeros((len(self.electrodes),int(tf-ti))))
            for i in range(len(self.electrodes)):
                self.exp_eeg_list[-1][i,:] = exp_dict[self.electrodes[i]][ti:tf]
                
        print(f'Training directories: {self.exp_path_list}')
        print("Number of experiments: {}".format(np.shape(np.array(self.exp_path_list))[0]))

        # Removing illegal values
        count = 0
        for i in range(len(self.exp_path_list)):
            count += np.count_nonzero(np.isnan(self.exp_eeg_list[i]))
        print(f'Total of {count} nan or inf')

        # Smoothing the dataset: idea from https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7038754/
        # I use a Savitzky–Golay filter with the following parameters: 4th (order) and 27 (frame length)
        if self.smooth:
            print('Smoothing the EEG data with a Savitzky–Golay filter')
            for i in range(len(self.exp_path_list)):
                self.smooth_exp_list.append(np.zeros_like(self.exp_eeg_list[i]))
                for j in range(len(self.electrodes)):
                    self.smooth_exp_list[i][j,:] = savgol_filter(self.exp_eeg_list[i][j,:], 5, 4) 
                    # window size 27, polynomial order 4
        else:
            self.smooth_exp_list = self.exp_eeg_list

        # Mean Centering
        if self.mean_center:
            print('Mean centering the filtered EEGs')
            for i in range(len(self.exp_path_list)):
                for j in range(len(self.electrodes)):
                    self.smooth_exp_list[i][j,:] -= np.mean(self.smooth_exp_list[i][j,:])

        # Time.frequency analysis: with Morlet continuous wavelet transform to provide a 
        # multiple time-scale representation of our postural mode dynamics. 
        # Ref: https://royalsocietypublishing.org/doi/full/10.1098/rsif.2014.0672
        print('Performing time-frequency analysis')
        for i in range(len(self.exp_path_list)):
            wav_exp, self.freq_channels = find_wav(np.transpose(self.smooth_exp_list[i]), 
                                              chan=num_channels, omega0=5, fps=samp_rate, 
                                                   fmin=min_frequency, fmax=max_frequency)
            self.wav_exp_list.append(np.transpose(wav_exp))
        print(f'Wavelet center frequencies: {self.freq_channels}')
                
        # Frame variance
        for exp_idx, exp in enumerate(self.wav_exp_list):
            self.frame_var_list.append(np.log10(np.var(exp, axis=0))) # FIX ME: LOG10(VAR)vs(SUM)?

        # Frame amplitude
        for exp_idx, exp in enumerate(self.wav_exp_list):
            self.frame_amp_list.append(np.log10(np.sum(exp, axis=0)))
        
        # Frame normalization
        print('Normalizing expression matrix columns')
        self.norm_wav_exp_list = self.wav_exp_list
        if self.normalize:
            for exp_idx, exp in enumerate(self.wav_exp_list):
                self.norm_wav_exp_list[exp_idx] =  self.wav_exp_list[exp_idx] / self.wav_exp_list[exp_idx].max(axis=0)
    
        # Set experiment and frame id for each instance
        self.exp_idx = [np.ones(shape=(exp.shape[1],)) * idx for (idx, exp) in
                        enumerate(self.exp_eeg_list)]
        self.exp_idx = np.concatenate(self.exp_idx, axis=0).astype(np.int)
        self.frame_idx = [np.arange(0, exp.shape[1]) for exp in self.exp_eeg_list]
        self.frame_idx = np.concatenate(self.frame_idx, axis=0).astype(np.int)
        
        # Set the behavior for each frame
        self.data_behav_concat = [np.zeros(shape=(exp.shape[1]), dtype=np.int64) for exp in
                                  self.exp_eeg_list]
        
        # Setting annotated behaviors (TODO)
        for d in self.data_behav_concat:
            d[:] = Behav.NONE.value
        for exp_idx in range(len(self.data_behav_concat)):
            for offset in range(self.clip_len // 2,
                                self.exp_eeg_list[exp_idx].shape[0] - self.clip_len // 2):
                offset_mid = offset + self.clip_len // 2
                experiment_path = self.exp_path_list[exp_idx]
                for label_gt in label_gt_list:
                    (offset_gt_start, offset_gt_end), label_gt_behav, folder_name = label_gt
                    if folder_name not in experiment_path:
                        continue
                    if offset_gt_start <= offset_mid < offset_gt_end:
                        if min(abs(offset_mid - offset_gt_start), abs(offset_mid - offset_gt_end)) > self.clip_len // 3:
                            self.data_behav_concat[exp_idx][offset_mid] = label_gt_behav.value

        # Setting the rest frames
        self.test_rest_mask = []
        for exp_idx, experiment in enumerate(self.exp_path_list):
            thr = filters.threshold_otsu(self.frame_amp_list[exp_idx], nbins=len(experiment) // 2) #use amplitude or variance?
            rest_mask = self.frame_amp_list[exp_idx] < thr
            self.test_rest_mask.append(rest_mask)
            self.data_behav_concat[exp_idx][rest_mask] = Behav.REST.value
        
        # Setting the boundary frames
        self.test_boundary_mask = []
        for exp_idx, experiment in enumerate(self.exp_path_list):
            boundary_mask = np.zeros(len(self.frame_amp_list[exp_idx]))
            boundary_mask[:bound_len] = 1; boundary_mask[-bound_len:] = 1
            bound_mask = boundary_mask > 0
            self.test_boundary_mask.append(bound_mask)
            self.data_behav_concat[exp_idx][bound_mask] = Behav.BOUNDARY.value

        # Concatenate the data
        self.data_concat = np.concatenate(self.norm_wav_exp_list, axis=1) # shape=(n_freq_channels,n_frames)
            
        self.data_behav_concat = np.concatenate(self.data_behav_concat, axis=0)
        print("Data concat shape {}".format(np.shape(np.array(self.data_concat))))
        print("Loaded {} frames from {} folders ".format(self.data_concat.shape[1], len(self.exp_path_list)))
        assert (self.frame_idx.shape[0] == self.exp_idx.shape[0] == self.data_concat.shape[1])
        
        self.dt = time.time() - self.time_start
        print(f'Data preprocessing completed. Time elapsed: {self.dt} seconds')
        
        # Apply rest and boundary masks
        self.data_mask = np.ones(np.shape(self.data_behav_concat), dtype=bool) #All trues
        if self.dump_rest:
            self.data_mask = np.logical_and(self.data_mask,self.data_behav_concat != Behav.REST.value)
        if self.no_bounds:
            self.data_mask = np.logical_and(self.data_mask,self.data_behav_concat != Behav.BOUNDARY.value)
            
        # Some useful output variables after masking the data
        self.preprocess_data = self.data_concat[:, self.data_mask]
        self.preprocess_data[np.logical_or(np.isnan(self.preprocess_data), np.isinf(self.preprocess_data))] = 0
        print(f'Preprocessed masked data shape: {np.shape(self.preprocess_data)}')
        self.frame_index = self.frame_idx[self.data_mask] 
        self.experminet_index = self.exp_idx[self.data_mask]
        self.frame_amplitudes = np.concatenate(self.frame_amp_list, axis=0)[self.data_mask]
        
        # Experiment dictionary
        self.preprocess_dict = {'Paths':self.exp_path_list,
                                'Data':{'EEG_list':self.exp_eeg_list, 'Smooth_EEG_list': self.smooth_exp_list,
                                        'Spect_list':self.wav_exp_list, 'Normalized_Spect_list':self.norm_wav_exp_list,
                                        'Exp_matrix':self.preprocess_data,
                                        'Frame_idx':self.frame_index, 'Experiment_idx':self.experminet_index,
                                        'Frame_amps':self.frame_amplitudes, 'Rest_mask':self.test_rest_mask,
                                        'Boundary_mask':self.test_boundary_mask}
                               }
        
            
                                
    # Functions to use:
    
    def _get_mean_(self):
        filename = 'mean_{}'.format("train")
        if os.path.isfile(filename + '.npy'):
            filename = filename + '.npy'
            print("Loading mean file {}".format(filename))
            d = np.load(filename).item()
            mean = d["mean"]
            std = d["std"]
        else:
            print("Calculating mean file")
            experiment_concat = np.concatenate(self.pts3d_exp_list, axis=0)
            mean = np.mean(experiment_concat, axis=0)
            std = np.std(experiment_concat, axis=0)
            print("Mean: {} std: {}".format(mean, std))
            np.save(filename, {"mean": mean, "std": std})
        print("Mean shape {}".format(mean.shape))
        return mean, std

    def _len_(self):
        return self.data_concat.shape[0]
    
    def _get_item_(self, idx):
        data = self.data_concat[idx]
        behav = self.data_behav_concat[idx]

        sample = {'data': data, 'behav': behav, 'idx': idx, 'fly_id': 0}
        
        return sample
    
    def _get_data_(self, exp_name=None, exp_idx=None, start_frame=0, end_frame=900):
        if exp_idx is None:
            for idx, exp_name_iter in enumerate(self.exp_path_list):
                if exp_name in exp_name_iter:
                    exp_idx = idx
                    break
        if exp_idx is None:
            print(exp_name)
        pts3d = self.pts3d_exp_list[exp_idx][start_frame:end_frame, :]
        data_mask_frame = np.logical_and(start_frame < self.frame_idx, self.frame_idx < end_frame)
        data_mask_exp = self.exp_idx == exp_idx
        data_mask = np.logical_and(data_mask_frame, data_mask_exp)

        d = self.data_concat[data_mask]
        
        return pts3d, d
    