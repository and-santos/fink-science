# Copyright 2020-2024 AstroLab Software
# Author: Andre Santos, Bernardo Fraga, Clecio de Bom
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
import numpy as np
import pandas as pd

from pyspark.sql.functions import pandas_udf, PandasUDFType
from pyspark.sql.types import ArrayType, FloatType

from fink_science import __file__
from fink_science.cats.utilities import norm_column
from fink_science.tester import spark_unit_tests


@pandas_udf(ArrayType(FloatType()), PandasUDFType.SCALAR)
@profile
def predict_nn(
    midpointTai: pd.Series,
    psFlux: pd.Series,
    psFluxErr: pd.Series,
    filterName: pd.Series,
    model=None,
) -> pd.Series:
    """Return broad predctions from a CBPF classifier model (cats general)

    Notes
    -----
    For the default model, one has the following mapping:
    class_dict = {
        0: 'SN-like',
        1: 'Fast',
        2: 'Long',
        3: 'Periodic',
        4: 'non-Periodic (AGN),
    }
    This model runs on Elasticc/Rubin data

    Parameters
    ----------
    midpointTai: spark DataFrame Column
        SNID JD Time (float)
    psFlux: spark DataFrame Column
        flux from LSST (float)
    psFluxErr: spark DataFrame Column
        flux error from LSST (float)
    filterName: spark DataFrame Column
        observed filter (string)
    model: spark DataFrame Column
        path to pre-trained Hierarchical Classifier model. (string)

    Returns
    -------
    preds: pd.Series
        preds is an pd.Series which contains a column
        with probabilities for broad classes shown in Elasticc data challenge.

    Examples
    --------
    >>> from fink_utils.spark.utils import concat_col
    >>> from pyspark.sql import functions as F
    >>> df = spark.read.format('parquet').load(elasticc_alert_sample)

    # Assuming random positions
    >>> df = df.withColumn('cdsxmatch', F.lit('Unknown'))
    >>> df = df.withColumn('roid', F.lit(0))

    # Required alert columns
    >>> what = ['midPointTai', 'psFlux', 'psFluxErr', 'filterName']

    # Use for creating temp name
    >>> prefix = 'c'
    >>> what_prefix = [prefix + i for i in what]

    # Append temp columns with historical + current measurements
    >>> for colname in what:
    ...     df = concat_col(
    ...         df, colname, prefix=prefix,
    ...         current='diaSource', history='prvDiaForcedSources')

    # Perform the fit + classification (default model)
    >>> args = [F.col(i) for i in what_prefix]
    >>> df = df.withColumn('preds', predict_nn(*args))
    >>> df = df.withColumn('argmax', F.expr('array_position(preds, array_max(preds)) - 1'))
    >>> df.filter(df['argmax'] == 0).count()
    49
    """
    import tensorflow as tf
    from tensorflow import keras

    filter_dict = {"u": 1, "g": 2, "r": 3, "i": 4, "z": 5, "Y": 6}

    mjd = []
    filters = []

    for i, mjds in enumerate(midpointTai):
        if len(mjds) > 0:
            filters.append(
                np.array([filter_dict[f] for f in filterName.to_numpy()[i]]).astype(
                    np.int16
                )
            )

            mjd.append(mjds - mjds[0])

    flux = psFlux.apply(lambda x: norm_column(x))
    error = psFluxErr.apply(lambda x: norm_column(x))

    flux = keras.utils.pad_sequences(
        flux, maxlen=395, value=-999.0, padding="post", dtype=np.float32
    )

    mjd = keras.utils.pad_sequences(
        mjd, maxlen=395, value=-999.0, padding="post", dtype=np.float32
    )

    error = keras.utils.pad_sequences(
        error, maxlen=395, value=-999.0, padding="post", dtype=np.float32
    )

    band = keras.utils.pad_sequences(
        filters, maxlen=395, value=0.0, padding="post", dtype=np.uint8
    )

    lc = np.concatenate(
        [mjd[..., None], flux[..., None], error[..., None], band[..., None]], axis=-1
    )

    if model is None:
        # Load pre-trained model
        curdir = os.path.dirname(os.path.abspath(__file__))
        model_path = curdir + "/data/models/cats_models/cats_small_nometa_serial.keras"
    else:
        model_path = model.to_numpy()[0]

    NN = tf.keras.models.load_model(model_path)

    preds = NN.predict([lc])

    return pd.Series(list(preds))


if __name__ == "__main__":
    """ Execute the test suite """

    globs = globals()
    path = os.path.dirname(__file__)

    elasticc_alert_sample = (
        "file://{}/data/alerts/elasticc_sample_seed0.parquet".format(path)
    )
    globs["elasticc_alert_sample"] = elasticc_alert_sample

    # Run the test suite
    spark_unit_tests(globs)
