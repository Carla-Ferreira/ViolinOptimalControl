import time

import biorbd
import numpy as np
from casadi import MX, vertcat, if_else, lt, gt

from biorbd_optim import (
    Instant,
    InterpolationType,
    Axe,
    OptimalControlProgram,
    Dynamics,
    Problem,
    CustomPlot,
    PlotType,
    ProblemType,
    Objective,
    Constraint,
    Bounds,
    QAndQDotBounds,
    InitialConditions,
    ShowResult,
)

from utils import Bow, Violin, Muscles


def xia_model_dynamic(states, controls, parameters, nlp):
    nbq = nlp["model"].nbQ()
    nbqdot = nlp["model"].nbQdot()
    nb_q_qdot = nbq + nbqdot

    q = states[:nbq]
    qdot = states[nbq:nb_q_qdot]
    active_fibers = states[nb_q_qdot : nb_q_qdot + nlp["nbMuscle"]]
    fatigued_fibers = states[nb_q_qdot + nlp["nbMuscle"] : nb_q_qdot + 2 * nlp["nbMuscle"]]
    resting_fibers = states[nb_q_qdot + 2 * nlp["nbMuscle"] :]

    residual_tau = controls[: nlp["nbTau"]]
    activation = controls[nlp["nbTau"] :]
    command = MX()

    comp = 0
    for i in range(nlp["model"].nbMuscleGroups()):
        for k in range(nlp["model"].muscleGroup(i).nbMuscles()):

            develop_factor = (
                nlp["model"].muscleGroup(i).muscle(k).characteristics().fatigueParameters().developFactor().to_mx()
            )
            recovery_factor = (
                nlp["model"].muscleGroup(i).muscle(k).characteristics().fatigueParameters().recoveryFactor().to_mx()
            )

            command = vertcat(
                command,
                if_else(
                    lt(active_fibers[comp], activation[comp]),
                    (
                        if_else(
                            gt(resting_fibers[comp], activation[comp] - active_fibers[comp]),
                            develop_factor * (activation[comp] - active_fibers[comp]),
                            develop_factor * resting_fibers[comp],
                        )
                    ),
                    recovery_factor * (active_fibers[comp] - activation[comp]),
                ),
            )
            comp += 1
        # todo lt "else" include equal ??
    restingdot = -command + Muscles.R * fatigued_fibers
    activatedot = command - Muscles.F * active_fibers
    fatiguedot = Muscles.F * active_fibers - Muscles.R * fatigued_fibers

    muscles_states = biorbd.VecBiorbdMuscleState(nlp["nbMuscle"])
    for k in range(nlp["nbMuscles"]):
        muscles_states[k].setActivation(active_fibers[k])
    # todo fix force max

    muscles_tau = nlp["model"].muscularJointTorque(muscles_states, q, qdot).to_mx()
    # todo get muscle forces and multiplicate them by activate [k] and same as muscularJointTorque
    tau = muscles_tau + residual_tau

    dxdt = MX(nlp["nx"], nlp["ns"])
    for i, f_ext in enumerate(nlp["external_forces"]):
        qddot = biorbd.Model.ForwardDynamics(nlp["model"], q, qdot, tau, f_ext).to_mx()
        dxdt[:, i] = vertcat(qdot, qddot, activatedot, fatiguedot, restingdot)
    return dxdt


def xia_model_configuration(ocp, nlp):
    Problem.configure_q_qdot(nlp, True, False)
    Problem.configure_tau(nlp, False, True)
    Problem.configure_muscles(nlp, False, True)

    x = MX()
    for i in range(nlp["nbMuscle"]):
        x = vertcat(x, MX.sym(f"Muscle_{nlp['muscleNames']}_active", 1, 1))
    for i in range(nlp["nbMuscle"]):
        x = vertcat(x, MX.sym(f"Muscle_{nlp['muscleNames']}_fatigue", 1, 1))
    for i in range(nlp["nbMuscle"]):
        x = vertcat(x, MX.sym(f"Muscle_{nlp['muscleNames']}_resting", 1, 1))

    nlp["x"] = vertcat(nlp["x"], x)
    nlp["var_states"]["muscles_active"] = nlp["nbMuscle"]
    nlp["var_states"]["muscles_fatigue"] = nlp["nbMuscle"]
    nlp["var_states"]["muscles_resting"] = nlp["nbMuscle"]
    nlp["nx"] = nlp["x"].rows()

    nb_q_qdot = nlp["nbQ"] + nlp["nbQdot"]
    nlp["plot"]["muscles_active"] = CustomPlot(
        lambda x, u, p: x[nb_q_qdot : nb_q_qdot + nlp["nbMuscle"]],
        plot_type=PlotType.INTEGRATED,
        legend=nlp["muscleNames"],
        color="r",
        ylim=[0, 1],
    )

    combine = "muscles_active"
    nlp["plot"]["muscles_fatigue"] = CustomPlot(
        lambda x, u, p: x[nb_q_qdot + nlp["nbMuscle"] : nb_q_qdot + 2 * nlp["nbMuscle"]],
        plot_type=PlotType.INTEGRATED,
        legend=nlp["muscleNames"],
        combine_to=combine,
        color="g",
        ylim=[0, 1],
    )
    nlp["plot"]["muscles_resting"] = CustomPlot(
        lambda x, u, p: x[nb_q_qdot + 2 * nlp["nbMuscle"] : nb_q_qdot + 3 * nlp["nbMuscle"]],
        plot_type=PlotType.INTEGRATED,
        legend=nlp["muscleNames"],
        combine_to=combine,
        color="b",
        ylim=[0, 1],
    )

    Problem.configure_forward_dyn_func(ocp, nlp, nlp["problem_type"]["dynamic"])


def xia_model_fibers_constraint(ocp, nlp, t, x, u, p):
    offset = nlp["model"].nbQ() + nlp["model"].nbQdot()
    nb_muscles = nlp["nbMuscles"]
    val = []
    for k in range(nb_muscles):
        val = vertcat(val, 1 - x[offset + k] - x[offset + k + nb_muscles] - x[offset + k + 2 * nb_muscles])
    return val


def prepare_ocp(biorbd_model_path="../models/BrasViolon.bioMod"):
    """
    Mix .bioMod and users data to call OptimalControlProgram constructor.
    :param biorbd_model_path: path to the .bioMod file.
    :param show_online_optim: bool which active live plot function.
    :return: OptimalControlProgram object.
    """

    # --- Options --- #
    biorbd_model = biorbd.Model(biorbd_model_path)
    muscle_activated_init, muscle_fatigued_init, muscle_resting_init = 0, 0, 1
    torque_min, torque_max, torque_init = -10, 10, 0
    muscle_states_ratio_min, muscle_states_ratio_max = 0, 1
    number_shooting_points = 30
    final_time = 0.5

    # --- String of the violin --- #
    violon_string = Violin("G")
    inital_bow_side = Bow("frog")

    # --- Objective --- #
    objective_functions = (
        {"type": Objective.Lagrange.MINIMIZE_MUSCLES_CONTROL, "weight": 10},
        {"type": Objective.Lagrange.MINIMIZE_TORQUE, "weight": 1},
        {"type": Objective.Lagrange.MINIMIZE_TORQUE, "controls_idx": [0, 1, 2, 3], "weight": 2000},
    )

    # --- Dynamics --- #
    problem_type = {"type": ProblemType.CUSTOM, "configure": xia_model_configuration, "dynamic": xia_model_dynamic}

    # --- Constraints --- #
    constraints = (
        {
            "type": Constraint.ALIGN_MARKERS,
            "instant": Instant.START,
            "first_marker_idx": Bow.frog_marker,
            "second_marker_idx": violon_string.bridge_marker,
        },
        {
            "type": Constraint.ALIGN_MARKERS,
            "instant": Instant.MID,
            "first_marker_idx": Bow.tip_marker,
            "second_marker_idx": violon_string.bridge_marker,
        },
        {
            "type": Constraint.ALIGN_MARKERS,
            "instant": Instant.END,
            "first_marker_idx": Bow.frog_marker,
            "second_marker_idx": violon_string.bridge_marker,
        },
        {
            "type": Constraint.ALIGN_SEGMENT_WITH_CUSTOM_RT,
            "instant": Instant.ALL,
            "segment_idx": Bow.segment_idx,
            "rt_idx": violon_string.rt_on_string,
        },
        {
            "type": Constraint.ALIGN_MARKER_WITH_SEGMENT_AXIS,
            "instant": Instant.ALL,
            "marker_idx": violon_string.bridge_marker,
            "segment_idx": Bow.segment_idx,
            "axis": (Axe.Y),
        },
        {
            "type": Constraint.ALIGN_MARKERS,
            "instant": Instant.ALL,
            "first_marker_idx": Bow.contact_marker,
            "second_marker_idx": violon_string.bridge_marker,
        },
        # {"type": Constraint.CUSTOM, "function": xia_model_fibers_constraint, "instant": Instant.ALL,},
        # TODO: add constraint about velocity in a marker of bow (start and end instant)
    )

    # --- External forces --- #
    external_forces = [np.repeat(violon_string.external_force[:, np.newaxis], number_shooting_points, axis=1)]

    # --- Path constraints --- #
    X_bounds = QAndQDotBounds(biorbd_model)

    X_bounds.min[biorbd_model.nbQ() :, [0, 2]] = 0
    X_bounds.max[biorbd_model.nbQ() :, [0, 2]] = 0
    # todo compare with

    muscle_states_bounds = Bounds(
        [muscle_states_ratio_min] * biorbd_model.nbMuscleTotal() * 3,
        [muscle_states_ratio_max] * biorbd_model.nbMuscleTotal() * 3,
    )
    X_bounds.concatenate(muscle_states_bounds)

    U_bounds = Bounds(
        [torque_min] * biorbd_model.nbGeneralizedTorque() + [muscle_states_ratio_min] * biorbd_model.nbMuscleTotal(),
        [torque_max] * biorbd_model.nbGeneralizedTorque() + [muscle_states_ratio_max] * biorbd_model.nbMuscleTotal(),
    )

    # --- Initial guess --- #
    optimal_initial_values = False
    if optimal_initial_values:
        X_init = InitialConditions(violon_string.x_init, InterpolationType.EACH_FRAME)
        U_init = InitialConditions(violon_string.u_init, InterpolationType.EACH_FRAME)
    else:
        X_init = InitialConditions(
            violon_string.initial_position()[inital_bow_side.side] + [0] * biorbd_model.nbQdot(),
            InterpolationType.CONSTANT,
        )
        U_init = InitialConditions(
            [torque_init] * biorbd_model.nbGeneralizedTorque() + [muscle_activated_init] * biorbd_model.nbMuscleTotal(),
            InterpolationType.CONSTANT,
        )

    muscle_states_init = InitialConditions(
        [muscle_activated_init] * biorbd_model.nbMuscleTotal()
        + [muscle_fatigued_init] * biorbd_model.nbMuscleTotal()
        + [muscle_resting_init] * biorbd_model.nbMuscleTotal(),
        InterpolationType.CONSTANT,
    )
    X_init.concatenate(muscle_states_init)

    # ------------- #

    return OptimalControlProgram(
        biorbd_model,
        problem_type,
        number_shooting_points,
        final_time,
        X_init,
        U_init,
        X_bounds,
        U_bounds,
        objective_functions,
        constraints,
        external_forces=external_forces,
        nb_threads=4,
    )


if __name__ == "__main__":
    ocp = prepare_ocp()

    # --- Solve the program --- #
    tic = time.time()
    sol, sol_iterations = ocp.solve(
        show_online_optim=True,
        return_iterations=True,
        options_ipopt={
            "tol": 1e-4,
            "max_iter": 3000,
            "ipopt.bound_push": 1e-10,
            "ipopt.bound_frac": 1e-10,
            "ipopt.hessian_approximation": "limited-memory",
        },
    )
    toc = time.time() - tic
    print(f"Time to solve : {toc}sec")

    t = time.localtime(time.time())
    date = f"{t.tm_year}_{t.tm_mon}_{t.tm_mday}"
    OptimalControlProgram.save(ocp, sol, f"results/{date}_upDown.bo")
    OptimalControlProgram.save_get_data(ocp, sol, f"results/{date}_upDown.bob", sol_iterations=sol_iterations)
    OptimalControlProgram.save_get_data(ocp, sol, f"results/{date}_upDown_interpolate.bob", interpolate_nb_frames=100)

    # --- Show results --- #
    result = ShowResult(ocp, sol)
    result.graphs()
