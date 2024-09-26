"""
Construct AREA_tmd_weights.csv.gz, a Tax-Calculator-style weights file
for FIRST_YEAR through LAST_YEAR for the specified sub-national AREA.

AREA prefix for state areas are the two lower-case character postal codes.
AREA prefix for congressional districts are the state prefix followed by
two digits (with a leading zero) identifying the district.  There are no
district files for states with only one congressional district.
"""

import sys
import time
import yaml
import numpy as np
import pandas as pd
from scipy.sparse import csr_matrix
from scipy.optimize import minimize, Bounds
import jax
import jax.numpy as jnp
from jax.experimental.sparse import BCOO
import taxcalc as tc
from tmd.storage import STORAGE_FOLDER
from tmd.areas import AREAS_FOLDER

FIRST_YEAR = 2021
LAST_YEAR = 2034
INFILE_PATH = STORAGE_FOLDER / "output" / "tmd.csv.gz"
WTFILE_PATH = STORAGE_FOLDER / "output" / "tmd_weights.csv.gz"
GFFILE_PATH = STORAGE_FOLDER / "output" / "tmd_growfactors.csv"
POPFILE_PATH = STORAGE_FOLDER / "input" / "cbo_population_forecast.yaml"

# target parameters:
TARGET_RATIO_TOLERANCE = 0.0005  # what is considered hitting the target
DUMP_ALL_TARGET_DEVIATIONS = False  # set to True only for diagnostic work

# regularization parameters:
DELTA_INIT_VALUE = 1.0e-9
DELTA_MAX_LOOPS = 5
DELTA_LOOP_DECREMENT = DELTA_INIT_VALUE / (DELTA_MAX_LOOPS - 1)

# optimization parameters:
OPTIMIZE_FTOL = 1e-9
OPTIMIZE_GTOL = 1e-9
OPTIMIZE_MAXITER = 5000
OPTIMIZE_IPRINT = 0  # 20 is a good diagnostic value; set to 0 for production
OPTIMIZE_RESULTS = False  # set to True to see complete optimization results

# taxcalc calculated variable cache files:
TAXCALC_AGI_CACHE = AREAS_FOLDER / "cache_agi.npy"


def valid_area(area: str):
    """
    Check validity of area string returning a boolean value.
    """
    # Data in the state_info dictionary is taken from the following document:
    # 2020 Census Apportionment Results, April 26, 2021,
    # Table C1. Number of Seats in
    #           U.S. House of Representatives by State: 1910 to 2020
    # https://www.census.gov/data/tables/2020/dec/2020-apportionment-data.html
    state_info = {
        "AL": {2020: 7, 2010: 7},
        "AK": {2020: 1, 2010: 1},
        "AZ": {2020: 9, 2010: 9},
        "AR": {2020: 4, 2010: 4},
        "CA": {2020: 52, 2010: 53},
        "CO": {2020: 8, 2010: 7},
        "CT": {2020: 5, 2010: 5},
        "DE": {2020: 1, 2010: 1},
        "FL": {2020: 28, 2010: 27},
        "GA": {2020: 14, 2010: 14},
        "HI": {2020: 2, 2010: 2},
        "ID": {2020: 2, 2010: 2},
        "IL": {2020: 17, 2010: 18},
        "IN": {2020: 9, 2010: 9},
        "IA": {2020: 4, 2010: 4},
        "KS": {2020: 4, 2010: 4},
        "KY": {2020: 6, 2010: 6},
        "LA": {2020: 6, 2010: 6},
        "ME": {2020: 2, 2010: 2},
        "MD": {2020: 8, 2010: 8},
        "MA": {2020: 9, 2010: 9},
        "MI": {2020: 13, 2010: 14},
        "MN": {2020: 8, 2010: 8},
        "MS": {2020: 4, 2010: 4},
        "MO": {2020: 8, 2010: 8},
        "MT": {2020: 2, 2010: 1},
        "NE": {2020: 3, 2010: 3},
        "NV": {2020: 4, 2010: 4},
        "NH": {2020: 2, 2010: 2},
        "NJ": {2020: 12, 2010: 12},
        "NM": {2020: 3, 2010: 3},
        "NY": {2020: 26, 2010: 27},
        "NC": {2020: 14, 2010: 13},
        "ND": {2020: 1, 2010: 1},
        "OH": {2020: 15, 2010: 16},
        "OK": {2020: 5, 2010: 5},
        "OR": {2020: 6, 2010: 5},
        "PA": {2020: 17, 2010: 18},
        "RI": {2020: 2, 2010: 2},
        "SC": {2020: 7, 2010: 7},
        "SD": {2020: 1, 2010: 1},
        "TN": {2020: 9, 2010: 9},
        "TX": {2020: 38, 2010: 36},
        "UT": {2020: 4, 2010: 4},
        "VT": {2020: 1, 2010: 1},
        "VA": {2020: 11, 2010: 11},
        "WA": {2020: 10, 2010: 10},
        "WV": {2020: 2, 2010: 3},
        "WI": {2020: 8, 2010: 8},
        "WY": {2020: 1, 2010: 1},
        # include three faux states used in testing:
        "XX": {2020: 1, 2010: 1},
        "YY": {2020: 1, 2010: 1},
        "ZZ": {2020: 1, 2010: 1},
    }
    # check state_info validity
    assert len(state_info) == 50 + 3
    total = {2010: 0, 2020: 0}
    for scode, seats in state_info.items():
        total[2010] += seats[2010]
        total[2020] += seats[2020]
    assert total[2010] == 435 + 3
    assert total[2020] == 435 + 3
    # conduct series of validity checks on specified area string
    all_ok = True
    # check that specified area string has expected length
    ok_length = len(area) == 2 or len(area) == 4
    if not ok_length:
        sys.stderr.write(f": area '{area}' is not two or four in length\n")
        all_ok = False
    # check that specified area begins with a two-character state code
    s_c = area[0:2]
    if not s_c.islower():
        sys.stderr.write(f": state code '{s_c}' must be all lower case\n")
        all_ok = False
    scode = s_c.upper()
    if scode not in state_info:
        sys.stderr.write(f": state '{s_c}' is unknown\n")
        all_ok = False
    # check congressional district number if appropriate
    if len(area) == 4:
        max_cdn = state_info[scode][2010]  # assuming CDs based on 2010 Census
        if max_cdn <= 1:
            sys.stderr.write(
                f": use area '{s_c}' for this one-district state\n"
            )
            all_ok = False
        else:
            cdn = int(area[2:4])
            if cdn <= 0:
                sys.stderr.write(f": cd number '{cdn}' is non-positive\n")
                all_ok = False
            if cdn > max_cdn:
                sys.stderr.write(f": cd number '{cdn}' exceeds {max_cdn}\n")
                all_ok = False
    return all_ok


def all_taxcalc_variables(write_cache):
    """
    Return all read and needed calc Tax-Calculator variables in pd.DataFrame.
    """
    vdf = pd.read_csv(INFILE_PATH)
    if TAXCALC_AGI_CACHE.exists():
        vdf["c00100"] = np.load(TAXCALC_AGI_CACHE)
    else:
        input_data = tc.Records(
            data=vdf,
            start_year=FIRST_YEAR,
            weights=str(WTFILE_PATH),
            gfactors=tc.GrowFactors(growfactors_filename=str(GFFILE_PATH)),
            adjust_ratios=None,
            exact_calculations=True,
        )
        sim = tc.Calculator(records=input_data, policy=tc.Policy())
        sim.calc_all()
        agi = sim.array("c00100")
        vdf["c00100"] = agi
        if write_cache:
            np.save(TAXCALC_AGI_CACHE, agi, allow_pickle=False)
    assert np.all(vdf.s006 > 0), "Not all weights are positive"
    return vdf


def prepared_data(area: str, vardf: pd.DataFrame):
    """
    Construct numpy 2-D target matrix and 1-D target array for
    specified area using specified vardf.  Also, compute initial
    weights scaling factor for specified area.  Return all three
    as a tuple.
    """
    national_population = (vardf.s006 * vardf.XTOT).sum()
    numobs = len(vardf)
    tdf = pd.read_csv(AREAS_FOLDER / "targets" / f"{area}_targets.csv")
    tm_tuple = ()
    ta_list = []
    row_num = 1
    for row in tdf.itertuples(index=False):
        row_num += 1
        line = f"{area}:L{row_num}"
        # construct target amount for this row
        unscaled_target = row.target
        if unscaled_target == 0:
            unscaled_target = 1.0
        scale = 1.0 / unscaled_target
        scaled_target = unscaled_target * scale
        ta_list.append(scaled_target)
        # confirm that row_num 2 contains the area population target
        if row_num == 2:
            bool_list = [
                row.varname == "XTOT",
                row.count == 0,
                row.scope == 0,
                row.agilo < -8e99,
                row.agihi > 8e99,
                row.fstatus == 0,
            ]
            assert all(
                bool_list
            ), f"{line} does not contain the area population target"
            initial_weights_scale = row.target / national_population
        # construct variable array for this target
        assert (
            row.count >= 0 and row.count <= 1
        ), f"count value {row.count} not in [0,1] range on {line}"
        if row.count == 0:
            unmasked_varray = vardf[row.varname].astype(float)
        else:
            unmasked_varray = (vardf[row.varname] > 0).astype(float)
        mask = np.ones(numobs, dtype=int)
        assert (
            row.scope >= 0 and row.scope <= 2
        ), f"scope value {row.scope} not in [0,2] range on {line}"
        if row.scope == 1:
            mask *= vardf.data_source == 1  # PUF records
        elif row.scope == 2:
            mask *= vardf.data_source == 0  # CPS records
        in_bin = (vardf.c00100 >= row.agilo) & (vardf.c00100 < row.agihi)
        mask *= in_bin
        assert (
            row.fstatus >= 0 and row.fstatus <= 5
        ), f"fstatus value {row.fstatus} not in [0,5] range on {line}"
        if row.fstatus > 0:
            mask *= vardf.MARS == row.fstatus
        scaled_masked_varray = mask * unmasked_varray * scale
        tm_tuple = tm_tuple + (scaled_masked_varray,)
    # construct target matrix and target array and return as tuple
    scale_factor = 1.0  # as high as 1e9 works just fine
    target_matrix = np.vstack(tm_tuple).T * scale_factor
    target_array = np.array(ta_list) * scale_factor
    return (
        target_matrix,
        target_array,
        initial_weights_scale,
    )


def target_misses(wght, target_matrix, target_array):
    """
    Return number of target misses for the specified weight array and a
    string containing size of each actual/expect target miss as a tuple.
    """
    actual = np.dot(wght, target_matrix)
    tratio = actual / target_array
    lob = 1.0 - TARGET_RATIO_TOLERANCE
    hib = 1.0 + TARGET_RATIO_TOLERANCE
    num = ((tratio < lob) | (tratio >= hib)).sum()
    mstr = ""
    if num > 0:
        for tnum, ratio in enumerate(tratio):
            if ratio < lob or ratio >= hib:
                mstr += (
                    f"  ::::TARGET{(tnum + 1):03d}:ACT/EXP,lob,hib="
                    f"  {ratio:.6f}  {lob:.6f}  {hib:.6f}\n"
                )
    return (num, mstr)


def target_rmse(wght, target_matrix, target_array, out, delta=None):
    """
    Return RMSE of the target deviations given specified arguments.
    """
    act = np.dot(wght, target_matrix)
    act_minus_exp = act - target_array
    ratio = act / target_array
    if DUMP_ALL_TARGET_DEVIATIONS:
        for tnum, ratio_ in enumerate(ratio):
            out.write(
                f"TARGET{(tnum + 1):03d}:ACT-EXP,ACT/EXP= "
                f"{act_minus_exp[tnum]:16.9e}, {ratio_:.3f}/n"
            )
    # show distribution of target ratios
    bins = [
        0.0,
        0.4,
        0.8,
        0.9,
        0.99,
        1.0 - TARGET_RATIO_TOLERANCE,
        1.0 + TARGET_RATIO_TOLERANCE,
        1.01,
        1.1,
        1.2,
        1.6,
        2.0,
        3.0,
        4.0,
        5.0,
        np.inf,
    ]
    tot = ratio.size
    out.write(f"DISTRIBUTION OF TARGET ACT/EXP RATIOS (n={tot}):\n")
    if delta is not None:
        out.write(f"  with REGULARIZATION_DELTA= {delta:e}\n")
    header = (
        "low bin ratio    high bin ratio"
        "    bin #    cum #     bin %     cum %\n"
    )
    out.write(header)
    cutout = pd.cut(ratio, bins, right=False, precision=6)
    count = pd.Series(cutout).value_counts().sort_index().to_dict()
    cum = 0
    for interval, num in count.items():
        cum += num
        if cum == 0:
            continue
        line = (
            f">={interval.left:13.6f}, <{interval.right:13.6f}:"
            f"  {num:6d}   {cum:6d}   {num/tot:7.2%}   {cum/tot:7.2%}\n"
        )
        out.write(line)
        if cum == tot:
            break
    # return RMSE of ACT-EXP targets
    return np.sqrt(np.mean(np.square(act_minus_exp)))


def objective_function(x, *args):
    """
    Objective function for minimization.
    Search for NOTE in this file for methodological details.
    https://web.stanford.edu/~boyd/cvxbook/bv_cvxbook.pdf#page=320
    """
    A, b, delta = args  # A is a jax sparse matrix
    ssq_target_deviations = jnp.sum(jnp.square(A @ x - b))
    ssq_weight_deviations = jnp.sum(jnp.square(x - 1.0))
    return ssq_target_deviations + delta * ssq_weight_deviations


JIT_FVAL_AND_GRAD = jax.jit(jax.value_and_grad(objective_function))


def weight_ratio_distribution(ratio, delta, out):
    """
    Print distribution of post-optimized to pre-optimized weight ratios.
    """
    bins = [
        0.0,
        1e-6,
        0.1,
        0.2,
        0.5,
        0.8,
        0.85,
        0.9,
        0.95,
        1.0,
        1.05,
        1.1,
        1.15,
        1.2,
        2.0,
        5.0,
        1e1,
        1e2,
        1e3,
        1e4,
        1e5,
        np.inf,
    ]
    tot = ratio.size
    out.write(f"DISTRIBUTION OF AREA/US WEIGHT RATIO (n={tot}):\n")
    out.write(f"  with REGULARIZATION_DELTA= {delta:e}\n")
    header = (
        "low bin ratio    high bin ratio"
        "    bin #    cum #     bin %     cum %\n"
    )
    out.write(header)
    cutout = pd.cut(ratio, bins, right=False, precision=6)
    count = pd.Series(cutout).value_counts().sort_index().to_dict()
    cum = 0
    for interval, num in count.items():
        cum += num
        if cum == 0:
            continue
        line = (
            f">={interval.left:13.6f}, <{interval.right:13.6f}:"
            f"  {num:6d}   {cum:6d}   {num/tot:7.2%}   {cum/tot:7.2%}\n"
        )
        out.write(line)
        if cum == tot:
            break
    ssqdev = np.sum(np.square(ratio - 1.0))
    out.write(f"SUM OF SQUARED AREA/US WEIGHT RATIO DEVIATIONS= {ssqdev:e}\n")


# -- High-level logic of the script:


def create_area_weights_file(
    area: str,
    write_log: bool = True,
    write_file: bool = True,
    write_cache: bool = True,
):
    """
    Create Tax-Calculator-style weights file for FIRST_YEAR through LAST_YEAR
    for specified area using information in area targets CSV file.
    Write log file if write_log=True, otherwise log is written to stdout.
    Write weights file if write_file=True, otherwise just do calculations.
    """
    # remove any existing log or weights files
    awpath = AREAS_FOLDER / "weights" / f"{area}_tmd_weights.csv.gz"
    awpath.unlink(missing_ok=True)
    logpath = AREAS_FOLDER / "weights" / f"{area}.log"
    logpath.unlink(missing_ok=True)

    # specify log output device
    if write_log:
        out = open(logpath, "w", encoding="utf-8")
    else:
        out = sys.stdout
    if write_file:
        out.write(f"CREATING WEIGHTS FILE FOR AREA {area} ...\n")
    else:
        out.write(f"DOING JUST WEIGHTS FILE CALCS FOR AREA {area} ...\n")

    # configure jax library
    jax.config.update("jax_platform_name", "cpu")  # ignore GPU/TPU if present
    jax.config.update("jax_enable_x64", True)  # use double precision floats

    # construct variable matrix and target array and weights_scale
    vdf = all_taxcalc_variables(write_cache)
    target_matrix, target_array, weights_scale = prepared_data(area, vdf)
    wght_us = np.array(vdf.s006 * weights_scale)
    out.write("INITIAL WEIGHTS STATISTICS:\n")
    out.write(f"sum of national weights = {vdf.s006.sum():e}\n")
    out.write(f"area weights_scale = {weights_scale:e}\n")
    num_weights = len(wght_us)
    num_targets = len(target_array)
    out.write(f"USING {area}_targets.csv FILE WITH {num_targets} TARGETS\n")
    rmse = target_rmse(wght_us, target_matrix, target_array, out)
    out.write(f"US_PROPORTIONALLY_SCALED_TARGET_RMSE= {rmse:.9e}\n")
    density = np.count_nonzero(target_matrix) / target_matrix.size
    out.write(f"target_matrix sparsity ratio = {(1.0 - density):.3f}\n")

    # optimize weight ratios by minimizing the sum of squared deviations
    # of area-to-us weight ratios from one such that the optimized ratios
    # hit all of the area targets
    #
    # NOTE: This a bi-criterion minimization problem that can be
    #       solved using regularization methods.  For background,
    #       consult Stephen Boyd and Lieven Vandenberghe, Convex
    #       Optimization, Cambridge University Press, 2004, in
    #       particular equation (6.9) on page 306 (see LINK below).
    #       Our problem is exactly the same as (6.9) except that
    #       we measure x deviations from one rather than from zero.
    # LINK: https://web.stanford.edu/~boyd/cvxbook/bv_cvxbook.pdf#page=320
    #
    A_dense = (target_matrix * wght_us[:, np.newaxis]).T
    A = BCOO.from_scipy_sparse(csr_matrix(A_dense))  # A is JAX sparse matrix
    b = target_array
    out.write(
        "OPTIMIZE WEIGHT RATIOS IN A REGULARIZATION LOOP\n"
        f"  where REGULARIZATION DELTA starts at {DELTA_INIT_VALUE:e}\n"
        f"  and where target_matrix.shape= {target_matrix.shape}\n"
    )
    # ... reduce value of regularization delta if not all targets are hit
    loop = 1
    delta = DELTA_INIT_VALUE
    wght0 = np.ones(num_weights)
    while loop <= DELTA_MAX_LOOPS:
        time0 = time.time()
        res = minimize(
            fun=JIT_FVAL_AND_GRAD,  # objective function and its gradient
            x0=wght0,  # initial guess for weight ratios
            jac=True,  # use gradient from JIT_FVAL_AND_GRAD function
            args=(A, b, delta),  # fixed arguments of objective function
            method="L-BFGS-B",  # use L-BFGS-B algorithm
            bounds=Bounds(0.0, np.inf),  # consider only non-negative weights
            options={
                "maxiter": OPTIMIZE_MAXITER,
                "ftol": OPTIMIZE_FTOL,
                "gtol": OPTIMIZE_GTOL,
                "iprint": OPTIMIZE_IPRINT,
                "disp": False if OPTIMIZE_IPRINT == 0 else None,
            },
        )
        time1 = time.time()
        wght_area = res.x * wght_us
        misses, minfo = target_misses(wght_area, target_matrix, target_array)
        if write_log:
            out.write(
                f"  ::loop,delta,misses:   {loop}" f"   {delta:e}   {misses}\n"
            )
        else:
            out.write(
                f"  ::loop,delta,misses,exectime(secs):   {loop}"
                f"   {delta:e}   {misses}   {(time1 - time0):.1f}\n"
            )
        if misses == 0 or res.success is False:
            break  # out of regularization delta loop
        # show magnitude of target misses
        out.write(minfo)
        # prepare for next regularization delta loop
        loop += 1
        delta -= DELTA_LOOP_DECREMENT
        if delta < 1e-20:
            delta = 0.0
    # ... show regularization/optimization results
    if write_log:
        res_summary = (
            f">>> final delta loop"
            f" iterations={res.nit}  success={res.success}\n"
            f">>> message: {res.message}\n"
            f">>> L-BFGS-B optimized objective function value: {res.fun:.9e}\n"
        )
    else:
        res_summary = (
            f">>> final delta loop exectime= {(time1-time0):.1f} secs"
            f"  iterations={res.nit}  success={res.success}\n"
            f">>> message: {res.message}\n"
            f">>> L-BFGS-B optimized objective function value: {res.fun:.9e}\n"
        )
    out.write(res_summary)
    if OPTIMIZE_RESULTS:
        out.write(">>> full final delta loop optimization results:\n")
        for key in res.keys():
            out.write(f"    {key}: {res.get(key)}\n")
    wght_area = res.x * wght_us
    misses = target_misses(wght_area, target_matrix, target_array)
    out.write(f"AREA-OPTIMIZED_TARGET_MISSES= {misses}\n")
    rmse = target_rmse(wght_area, target_matrix, target_array, out, delta)
    out.write(f"AREA-OPTIMIZED_TARGET_RMSE= {rmse:.9e}\n")
    weight_ratio_distribution(res.x, delta, out)

    if write_log:
        out.close()
    if not write_file:
        return 0

    # write area weights file extrapolating using national population forecast
    # ... get population forecast
    with open(POPFILE_PATH, "r", encoding="utf-8") as pfile:
        pop = yaml.safe_load(pfile.read())
    # ... set FIRST_YEAR weights
    weights = wght_area * 100  # scale up weights by 100 for Tax-Calculator
    # ... construct dictionary of scaled-up weights by year
    wdict = {f"WT{FIRST_YEAR}": weights}
    cum_pop_growth = 1.0
    for year in range(FIRST_YEAR + 1, LAST_YEAR + 1):
        annual_pop_growth = pop[year] / pop[year - 1]
        cum_pop_growth *= annual_pop_growth
        wght = weights.copy() * cum_pop_growth
        wdict[f"WT{year}"] = wght
    # ... write rounded integer scaled-up weights to CSV-formatted file
    wdf = pd.DataFrame.from_dict(wdict)
    wdf.to_csv(awpath, index=False, float_format="%.0f", compression="gzip")

    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.stderr.write(
            "ERROR: exactly one command-line argument is required\n"
        )
        sys.exit(1)
    area_code = sys.argv[1]
    if not valid_area(area_code):
        sys.stderr.write(f"ERROR: {area_code} is not valid\n")
        sys.exit(1)
    tfile = f"{area_code}_targets.csv"
    target_file = AREAS_FOLDER / "targets" / tfile
    if not target_file.exists():
        sys.stderr.write(
            f"ERROR: {tfile} file not in tmd/areas/targets folder\n"
        )
        sys.exit(1)
    RCODE = create_area_weights_file(
        area_code,
        write_log=False,
        write_file=True,
        write_cache=False,
    )
    sys.exit(RCODE)
