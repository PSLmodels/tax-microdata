"""
This module provides utilities for working with Tax-Calculator.
"""

import taxcalc as tc
import numpy as np
import pandas as pd
from tax_microdata_benchmarking.storage import STORAGE_FOLDER
import yaml


with open(STORAGE_FOLDER / "input" / "taxcalc_variable_metadata.yaml") as f:
    taxcalc_variable_metadata = yaml.safe_load(f)


def get_tc_variable_description(variable: str) -> str:
    """
    Get the description of a Tax-Calculator variable.

    Args:
        variable (str): The name of the variable.

    Returns:
        str: The description of the variable.
    """
    if variable in taxcalc_variable_metadata.get("read", {}):
        return taxcalc_variable_metadata["read"][variable]["desc"]
    elif variable in taxcalc_variable_metadata.get("calc", {}):
        return taxcalc_variable_metadata["calc"][variable]["desc"]


def get_tc_is_input(variable: str) -> bool:
    """
    Get the type (whether input or not) of a Tax-Calculator variable.

    Args:
        variable (str): The name of the variable.

    Returns:
        bool: Whether the variable is an input.
    """
    if variable in taxcalc_variable_metadata.get("read", {}):
        return True
    elif variable in taxcalc_variable_metadata.get("calc", {}):
        return False


def add_taxcalc_outputs(
    flat_file: pd.DataFrame,
    time_period: int,
    reform: dict = None,
    weights=None,
    growfactors=None,
) -> pd.DataFrame:
    """
    Run a flat file through Tax-Calculator.

    Args:
        flat_file (pd.DataFrame): The flat file to run through Tax-Calculator.
        time_period (int): The year to run the simulation for.
        reform (dict, optional): The reform to apply. Defaults to None.

    Returns:
        pd.DataFrame: The Tax-Calculator output.
    """
    input_data = tc.Records(
        data=flat_file,
        start_year=time_period,
        weights=weights,
        gfactors=growfactors,
    )
    policy = tc.Policy()
    if reform:
        policy.implement_reform(reform)
    simulation = tc.Calculator(records=input_data, policy=policy)
    simulation.calc_all()
    output = simulation.dataframe(None, all_vars=True)
    assert np.allclose(output.s006, flat_file.s006)
    return output


te_reforms = {
    "cg_tax_preference": {"CG_nodiff": {"2023": True}},
    "ctc": {"CTC_c": {"2023": 0}, "ODC_c": {"2023": 0}, "ACTC_c": {"2023": 0}},
    "eitc": {"EITC_c": {"2023": [0, 0, 0, 0]}},
    "niit": {"NIIT_rt": {"2023": 0}},
    "qbid": {"PT_qbid_rt": {"2023": 0}},
    "salt": {"ID_AllTaxes_hc": {"2023": 1}},
    "social_security_partial_taxability": {"SS_all_in_agi": {"2023": True}},
}


def get_tax_expenditure_results(
    flat_file: pd.DataFrame,
    time_period: int,
) -> dict:

    growfactors = tc.GrowFactors(
        str(STORAGE_FOLDER / "output" / "tmd_growfactors.csv")
    )
    weights = str(STORAGE_FOLDER / "output" / "tmd_weights.csv.gz")
    baseline = add_taxcalc_outputs(
        flat_file, time_period, weights=weights, growfactors=growfactors
    )

    tax_revenue_baseline = (baseline.combined * baseline.s006).sum() / 1e9

    te_results = {}
    for reform_name, reform in te_reforms.items():
        reform_results = add_taxcalc_outputs(flat_file, time_period, reform)
        tax_revenue_reform = (
            reform_results.combined * reform_results.s006
        ).sum() / 1e9
        revenue_effect = tax_revenue_baseline - tax_revenue_reform
        te_results[reform_name] = round(-revenue_effect, 1)

    return te_results
