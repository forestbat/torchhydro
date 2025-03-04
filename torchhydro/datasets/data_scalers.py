import copy
import json
import os
import pickle as pkl
import shutil
import pint_xarray  # noqa: F401
import xarray as xr
from hydrodataset import HydroDataset
import numpy as np
from sklearn.preprocessing import (
    StandardScaler,
    RobustScaler,
    MinMaxScaler,
    MaxAbsScaler,
)

from hydroutils.hydro_stat import (
    cal_stat_prcp_norm,
    cal_stat_gamma,
    cal_stat,
)

from torchhydro.datasets.data_utils import (
    _trans_norm,
    _prcp_norm,
    wrap_t_s_dict,
    unify_streamflow_unit,
)

SCALER_DICT = {
    "StandardScaler": StandardScaler,
    "RobustScaler": RobustScaler,
    "MinMaxScaler": MinMaxScaler,
    "MaxAbsScaler": MaxAbsScaler,
}


class ScalerHub(object):
    """
    A class for Scaler
    """

    def __init__(
        self,
        target_vars: np.array,
        relevant_vars: np.array,
        constant_vars: np.array = None,
        data_params: dict = None,
        loader_type: str = None,
        **kwargs,
    ):
        """
        Perform normalization

        Parameters
        ----------
        target_vars
            output variables
        relevant_vars
            dynamic input variables
        constant_vars
            static input variables
        other_vars
            other required variables
        data_params
            parameters for reading data
        loader_type
            train, valid or test
        kwargs
            other optional parameters for ScalerHub
        """
        self.data_params = data_params
        norm_keys = ["target_vars", "relevant_vars", "constant_vars"]
        norm_dict = {}
        scaler_type = data_params["scaler"]
        if scaler_type == "DapengScaler":
            assert "data_source" in list(kwargs.keys())
            gamma_norm_cols = data_params["scaler_params"]["gamma_norm_cols"]
            prcp_norm_cols = data_params["scaler_params"]["prcp_norm_cols"]
            pbm_norm = data_params["scaler_params"]["pbm_norm"]
            scaler = DapengScaler(
                target_vars,
                relevant_vars,
                constant_vars,
                data_params,
                loader_type,
                kwargs["data_source"],
                prcp_norm_cols=prcp_norm_cols,
                gamma_norm_cols=gamma_norm_cols,
                pbm_norm=pbm_norm,
            )
            x, y, c = scaler.load_data()
            self.target_scaler = scaler
        elif scaler_type in SCALER_DICT.keys():
            # TODO: not fully tested, espacially for pbm models
            all_vars = [target_vars, relevant_vars, constant_vars]
            for i in range(len(all_vars)):
                data_tmp = all_vars[i]
                scaler = SCALER_DICT[scaler_type]()
                if data_tmp.ndim == 3:
                    # for forcings and outputs
                    num_instances, num_time_steps, num_features = data_tmp.shape
                    data_tmp = data_tmp.to_numpy().reshape(-1, num_features)
                    save_file = os.path.join(
                        data_params["test_path"], f"{norm_keys[i]}_scaler.pkl"
                    )
                    if loader_type == "train" and data_params["stat_dict_file"] is None:
                        data_norm = scaler.fit_transform(data_tmp)
                        # Save scaler in test_path for valid/test
                        with open(save_file, "wb") as outfile:
                            pkl.dump(scaler, outfile)
                    else:
                        if data_params["stat_dict_file"] is not None:
                            shutil.copy(data_params["stat_dict_file"], save_file)
                        if not os.path.isfile(save_file):
                            raise FileNotFoundError(
                                "Please genereate xx_scaler.pkl file"
                            )
                        with open(save_file, "rb") as infile:
                            scaler = pkl.load(infile)
                            data_norm = scaler.transform(data_tmp)
                    data_norm = data_norm.reshape(
                        num_instances, num_time_steps, num_features
                    )
                else:
                    # for attributes
                    save_file = os.path.join(
                        data_params["test_path"], f"{norm_keys[i]}_scaler.pkl"
                    )
                    if loader_type == "train" and data_params["stat_dict_file"] is None:
                        data_norm = scaler.fit_transform(data_tmp)
                        # Save scaler in test_path for valid/test
                        with open(save_file, "wb") as outfile:
                            pkl.dump(scaler, outfile)
                    else:
                        if data_params["stat_dict_file"] is not None:
                            shutil.copy(data_params["stat_dict_file"], save_file)
                        assert os.path.isfile(save_file)
                        with open(save_file, "rb") as infile:
                            scaler = pkl.load(infile)
                            data_norm = scaler.transform(data_tmp)
                norm_dict[norm_keys[i]] = data_norm
                if i == 0:
                    self.target_scaler = scaler
            x = norm_dict["relevant_vars"]
            y = norm_dict["target_vars"]
            c = norm_dict["constant_vars"]
        else:
            raise NotImplementedError(
                "We don't provide this Scaler now!!! Please choose another one: DapengScaler or key in SCALER_DICT"
            )
        print("Finish Normalization\n")
        self.x = x
        self.y = y
        self.c = c


class DapengScaler(object):
    def __init__(
        self,
        target_vars: np.array,
        relevant_vars: np.array,
        constant_vars: np.array,
        data_params: dict,
        loader_type: str,
        data_source: HydroDataset,
        other_vars: dict = None,
        prcp_norm_cols=None,
        gamma_norm_cols=None,
        pbm_norm=False,
    ):
        """
        The normalization and denormalization methods from Dapeng's 1st WRR paper.
        Some use StandardScaler, and some use special norm methods

        Parameters
        ----------
        target_vars
            output variables
        relevant_vars
            input dynamic variables
        constant_vars
            input static variables
        data_params
            data parameter config in data source
        loader_type
            train/valid/test
        data_source
            all config about data source
        other_vars
            if more input are needed, list them in other_vars
        prcp_norm_cols
            data items which use _prcp_norm method to normalize
        gamma_norm_cols
            data items which use log(\sqrt(x)+.1) method to normalize
        pbm_norm
            if true, use pbm_norm method to normalize; the output of pbms is not normalized data, so its inverse is different.
        """
        if prcp_norm_cols is None:
            prcp_norm_cols = [
                "streamflow",
            ]
        if gamma_norm_cols is None:
            gamma_norm_cols = [
                "prcp",
                "pr",
                "total_precipitation",
                "pet",
                "potential_evaporation",
                "PET",
            ]
        self.data_target = target_vars
        self.data_forcing = relevant_vars
        self.data_attr = constant_vars
        self.data_source = data_source
        self.data_params = data_params
        self.t_s_dict = wrap_t_s_dict(data_source, data_params, loader_type)
        self.data_other = other_vars
        self.prcp_norm_cols = prcp_norm_cols
        self.gamma_norm_cols = gamma_norm_cols
        # both prcp_norm_cols and gamma_norm_cols use log(\sqrt(x)+.1) method to normalize
        self.log_norm_cols = gamma_norm_cols + prcp_norm_cols
        self.pbm_norm = pbm_norm
        # save stat_dict of training period in test_path for valid/test
        stat_file = os.path.join(data_params["test_path"], "dapengscaler_stat.json")
        # for testing sometimes such as pub cases, we need stat_dict_file from trained dataset
        if loader_type == "train" and data_params["stat_dict_file"] is None:
            self.stat_dict = self.cal_stat_all()
            with open(stat_file, "w") as fp:
                json.dump(self.stat_dict, fp)
        else:
            # for valid/test, we need to load stat_dict from train
            if data_params["stat_dict_file"] is not None:
                # we used a assigned stat file, typically for PUB exps
                shutil.copy(data_params["stat_dict_file"], stat_file)
            assert os.path.isfile(stat_file)
            with open(stat_file, "r") as fp:
                self.stat_dict = json.load(fp)

    def inverse_transform(self, target_values):
        """
        Denormalization for output variables

        Parameters
        ----------
        target_values
            output variables

        Returns
        -------
        np.array
            denormalized predictions
        """
        stat_dict = self.stat_dict
        target_cols = self.data_params["target_cols"]
        if self.pbm_norm:
            # for pbm's output, its unit is mm/day, so we don't need to recover its unit
            pred = target_values
        else:
            pred = _trans_norm(
                target_values,
                target_cols,
                stat_dict,
                log_norm_cols=self.log_norm_cols,
                to_norm=False,
            )
            for i in range(len(self.data_params["target_cols"])):
                var = self.data_params["target_cols"][i]
                if var in self.prcp_norm_cols:
                    mean_prep = self.data_source.read_mean_prcp(
                        self.t_s_dict["sites_id"]
                    )
                    pred.loc[dict(variable=var)] = _prcp_norm(
                        pred.sel(variable=var).to_numpy(),
                        mean_prep.to_array().to_numpy().T,
                        to_norm=False,
                    )
        # add attrs for units
        pred.attrs.update(self.data_target.attrs)
        # trans to xarray dataset
        pred_ds = pred.to_dataset(dim="variable")
        pred_ds = pred_ds.pint.quantify(pred_ds.attrs["units"])
        area = self.data_source.read_area(self.t_s_dict["sites_id"])
        return unify_streamflow_unit(pred_ds, area=area, inverse=True)

    def cal_stat_all(self):
        """
        Calculate statistics of outputs(streamflow etc), and inputs(forcing and attributes)

        Returns
        -------
        dict
            a dict with statistic values
        """
        # streamflow
        target_cols = self.data_params["target_cols"]
        stat_dict = {}
        for i in range(len(target_cols)):
            var = target_cols[i]
            if var in self.prcp_norm_cols:
                mean_prep = self.data_source.read_mean_prcp(self.t_s_dict["sites_id"])
                stat_dict[var] = cal_stat_prcp_norm(
                    self.data_target.sel(variable=var).to_numpy(),
                    mean_prep.to_array().to_numpy().T,
                )
            elif var in self.gamma_norm_cols:
                stat_dict[var] = cal_stat_gamma(
                    self.data_target.sel(variable=var).to_numpy()
                )
            else:
                stat_dict[var] = cal_stat(self.data_target.sel(variable=var).to_numpy())

        # forcing
        forcing_lst = self.data_params["relevant_cols"]
        x = self.data_forcing
        for k in range(len(forcing_lst)):
            var = forcing_lst[k]
            if var in self.gamma_norm_cols:
                stat_dict[var] = cal_stat_gamma(x.sel(variable=var).to_numpy())
            else:
                stat_dict[var] = cal_stat(x.sel(variable=var).to_numpy())

        # const attribute
        attr_data = self.data_attr
        attr_lst = self.data_params["constant_cols"]
        for k in range(len(attr_lst)):
            var = attr_lst[k]
            stat_dict[var] = cal_stat(attr_data.sel(variable=var).to_numpy())

        return stat_dict

    def get_data_obs(self, to_norm: bool = True) -> np.array:
        """
        Get observation values

        Parameters
        ----------
        rm_nan
            if true, fill NaN value with 0
        to_norm
            if true, perform normalization

        Returns
        -------
        np.array
            the output value for modeling
        """
        stat_dict = self.stat_dict
        data = self.data_target
        out = xr.full_like(data, np.nan)
        # if we don't set a copy() here, the attrs of data will be changed, which is not our wish
        out.attrs = copy.deepcopy(data.attrs)
        target_cols = self.data_params["target_cols"]
        for i in range(len(target_cols)):
            var = target_cols[i]
            if var in self.prcp_norm_cols:
                mean_prep = self.data_source.read_mean_prcp(self.t_s_dict["sites_id"])
                out.loc[dict(variable=var)] = _prcp_norm(
                    data.sel(variable=var).to_numpy(),
                    mean_prep.to_array().to_numpy().T,
                    to_norm=True,
                )
                out.attrs["units"][var] = "dimensionless"
        out = _trans_norm(
            out,
            target_cols,
            stat_dict,
            log_norm_cols=self.log_norm_cols,
            to_norm=to_norm,
        )
        return out

    def get_data_ts(self, to_norm=True) -> np.array:
        """
        Get dynamic input data

        Parameters
        ----------
        rm_nan
            if true, fill NaN value with 0
        to_norm
            if true, perform normalization

        Returns
        -------
        np.array
            the dynamic inputs for modeling
        """
        stat_dict = self.stat_dict
        var_lst = self.data_params["relevant_cols"]
        data = self.data_forcing
        data = _trans_norm(
            data, var_lst, stat_dict, log_norm_cols=self.log_norm_cols, to_norm=to_norm
        )
        return data

    def get_data_const(self, to_norm=True) -> np.array:
        """
        Attr data and normalization

        Parameters
        ----------
        rm_nan
            if true, fill NaN value with 0
        to_norm
            if true, perform normalization

        Returns
        -------
        np.array
            the static inputs for modeling
        """
        stat_dict = self.stat_dict
        var_lst = self.data_params["constant_cols"]
        data = self.data_attr
        data = _trans_norm(data, var_lst, stat_dict, to_norm=to_norm)
        return data

    def load_data(self):
        """
        Read data and perform normalization for DL models

        Returns
        -------
        tuple
            x: 3-d  gages_num*time_num*var_num
            y: 3-d  gages_num*time_num*1
            c: 2-d  gages_num*var_num
        """
        x = self.get_data_ts()
        y = self.get_data_obs()
        c = self.get_data_const()
        return x, y, c
