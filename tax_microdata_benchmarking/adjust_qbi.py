import numpy as np
import pandas as pd
from scipy.optimize import minimize, bisect
import taxcalc as tc


def add_pt_w2_wages(df, verbose: bool = True):
    """
    Add 2021 pass-through W-2 wages to the flat file.

    Args:
        df (pd.DataFrame): the 2021 DataFrame to which adding W-2 wages

    Returns:
        tuple containing:
          pd.DataFrame: the 2021 DataFrame with pass-through W-2 wages added
          pt_w2_wages_scale: rounded to four decimal digits
    """
    QBID_TOTAL = 205.8  # from 2021 SOI tabulations (in $billions)
    qbi = np.maximum(0, df.e00900 + df.e26270 + df.e02100 + df.e27200)

    # Solve for scale to match the QBID_TARGET

    def deduction_loss(scale):
        input_data = df.copy()
        input_data["PT_binc_w2_wages"] = qbi * scale
        input_data = tc.Records(
            data=input_data,
            start_year=2021,
            gfactors=None,
            weights=None,
            adjust_ratios=None,
            exact_calculations=True,
        )
        sim = tc.Calculator(records=input_data, policy=tc.Policy())
        sim.calc_all()
        qbided = (sim.array("qbided") * df.s006).sum() / 1e9
        dev = qbided - QBID_TOTAL
        if verbose:
            print(f"scale: {scale:8.6f}, dev: {dev:6.2f}, tot: {qbided:.2f}")
        return dev

    scale = bisect(deduction_loss, 0.1, 0.5, rtol=0.001)
    rounded_scale = round(scale, 4)
    print(f"Final (rounded) scale: {rounded_scale:.4f}")
    df["PT_binc_w2_wages"] = qbi * rounded_scale
    return (df, rounded_scale)


if __name__ == "__main__":
    from tax_microdata_benchmarking.create_flat_file import (
        create_stacked_flat_file,
    )

    df = create_stacked_flat_file(2021)
    (df, scale) = add_pt_w2_wages(df)
