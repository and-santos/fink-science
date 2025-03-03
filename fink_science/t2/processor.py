# Copyright 2022-2024 AstroLab Software
# Author: Tarek Allam, Julien Peloton
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from line_profiler import profile

import os

from pyspark.sql.functions import pandas_udf, PandasUDFType
from pyspark.sql.types import StringType, FloatType, MapType

import pandas as pd
import numpy as np

from astronet.preprocess import generate_gp_all_objects
from astronet.preprocess import robust_scale

from fink_utils.data.utils import format_data_as_snana

from fink_science import __file__
from fink_science.t2.utilities import get_lite_model
from fink_science.t2.utilities import apply_selection_cuts_ztf
from fink_science.t2.utilities import extract_maxclass
from fink_science.t2.utilities import T2_COLS

from fink_science.tester import spark_unit_tests


@pandas_udf(StringType(), PandasUDFType.SCALAR)
def maxclass(dic):
    """Extract t-he class with max probability"""
    max_class_series = dic.apply(lambda x: extract_maxclass(x))
    return max_class_series


@pandas_udf(MapType(StringType(), FloatType()), PandasUDFType.SCALAR)
@profile
def t2(
    candid, jd, fid, magpsf, sigmapsf, roid, cdsxmatch, jdstarthist, model_name=None
) -> pd.Series:
    """Return vector of probabilities from T2

    Parameters
    ----------
    candid: Spark DataFrame Column
        Candidate IDs (int64)
    jd: Spark DataFrame Column
        JD times (float)
    fid: Spark DataFrame Column
        Filter IDs (int)
    magpsf, sigmapsf: Spark DataFrame Columns
        Magnitude from PSF-fit photometry, and 1-sigma error
    model_name: Spark DataFrame Column, optional
        T2 pre-trained model. Currently available:
            * tinho

    Returns
    -------
    probabilities: dictionnary (class>str, prob>float)
        Probability between 0 (non-Ia) and 1 (Ia).

    Examples
    --------
    >>> from fink_science.xmatch.processor import xmatch_cds
    >>> from fink_science.asteroids.processor import roid_catcher
    >>> from fink_utils.spark.utils import concat_col
    >>> from pyspark.sql import functions as F

    >>> df = spark.read.load(ztf_alert_sample)

    # Add SIMBAD field
    >>> df = xmatch_cds(df)

    # Required alert columns
    >>> what = ['jd', 'fid', 'magpsf', 'sigmapsf']

    # Use for creating temp name
    >>> prefix = 'c'
    >>> what_prefix = [prefix + i for i in what]

    # Append temp columns with historical + current measurements
    >>> for colname in what:
    ...    df = concat_col(df, colname, prefix=prefix)

    # Add SSO field
    >>> args_roid = [
    ...    'cjd', 'cmagpsf',
    ...    'candidate.ndethist', 'candidate.sgscore1',
    ...    'candidate.ssdistnr', 'candidate.distpsnr1']
    >>> df = df.withColumn('roid', roid_catcher(*args_roid))

    # Perform the fit + classification (default t2 model)
    >>> args = ['candid', 'cjd', 'cfid', 'cmagpsf', 'csigmapsf']
    >>> args += [F.col('roid'), F.col('cdsxmatch'), F.col('candidate.jdstarthist')]
    >>> df = df.withColumn('t2', t2(*args))

    >>> df = df.withColumn('maxClass', maxclass('t2'))

    >>> df.filter(df['maxClass'] == 'SNIa').count()
    0
    """
    default = {k: -1.0 for k in T2_COLS}
    to_return = np.array([default for i in range(len(jd))])

    mask = apply_selection_cuts_ztf(magpsf, cdsxmatch, jd, jdstarthist, roid)

    if len(jd[mask]) == 0:
        return pd.Series(to_return)

    ZTF_FILTER_MAP = {1: "ztfg", 2: "ztfr", 3: "ztfi"}

    ZTF_PB_WAVELENGTHS = {
        "ztfg": 4804.79,
        "ztfr": 6436.92,
        "ztfi": 7968.22,
    }

    # Rescale dates to _start_ at 0
    dates = jd.apply(lambda x: [x[0] - i for i in x])

    pdf = format_data_as_snana(
        dates, magpsf, sigmapsf, fid, candid, mask, filter_conversion_dic=ZTF_FILTER_MAP
    )

    pdf = pdf.rename(
        columns={
            "SNID": "object_id",
            "MJD": "mjd",
            "FLUXCAL": "flux",
            "FLUXCALERR": "flux_error",
            "FLT": "filter",
        }
    )

    pdf = pdf.dropna()
    pdf = pdf.reset_index()

    if model_name is not None:
        # take the first element of the Series
        model = get_lite_model(model_name=model_name.to_numpy()[0])
    else:
        # Load default pre-trained model
        model = get_lite_model()

    vals = []
    for candid_ in candid[mask].to_numpy():
        # one object at a time
        sub = pdf[pdf["object_id"] == candid_]

        # Need all filters
        if len(np.unique(sub["filter"])) != 2:
            vals.append(default)
            continue

        # one object at a time
        df_gp_mean = generate_gp_all_objects(
            [candid_], sub, pb_wavelengths=ZTF_PB_WAVELENGTHS
        )

        cols = set(ZTF_PB_WAVELENGTHS.keys()) & set(df_gp_mean.columns)
        robust_scale(df_gp_mean, cols)
        X = df_gp_mean[cols]
        X = np.asarray(X).astype("float32")
        X = np.expand_dims(X, axis=0)

        y_preds = model.predict(X)

        values = y_preds.tolist()
        predictions = dict(zip(T2_COLS, values[0]))
        vals.append(predictions)

    to_return[mask] = vals

    # return vector of probabilities
    return pd.Series(to_return)


if __name__ == "__main__":
    """ Execute the test suite """

    globs = globals()
    path = os.path.dirname(__file__)

    ztf_alert_sample = "file://{}/data/alerts/datatest".format(path)
    globs["ztf_alert_sample"] = ztf_alert_sample

    # Run the test suite
    spark_unit_tests(globs)
