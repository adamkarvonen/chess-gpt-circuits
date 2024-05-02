from tqdm import tqdm
import pickle
import torch
import einops

from circuits.utils import (
    get_ae_bundle,
    collect_activations_batch,
)
import circuits.chess_utils as chess_utils

# Dimension key (from https://medium.com/@NoamShazeer/shape-suffixes-good-coding-style-f836e72e24fd):
# F  = features and minibatch size depending on the context (maybe this is stupid)
# B = batch_size
# L = seq length (context length)
# T = thresholds
# R = rows (or cols)
# C = classes for one hot encoding


def initialize_results_dict(
    custom_functions: list[callable], num_thresholds: int, num_features: int
) -> dict:
    """For every function for every threshold for every feature, we keep track of the counts for every element
    in the state stack, along with the activations counts. This is done in parallel to make it fast.
    """
    results = {}
    for custom_function in custom_functions:
        results[custom_function.__name__] = {}
        config = chess_utils.config_lookup[custom_function.__name__]
        num_classes = chess_utils.get_num_classes(config)

        results[custom_function.__name__] = {}
        on_tracker_TFRRC = torch.zeros(
            num_thresholds, num_features, config.num_rows, config.num_cols, num_classes
        ).to(device)
        results[custom_function.__name__]["on"] = on_tracker_TFRRC
        results[custom_function.__name__]["off"] = on_tracker_TFRRC.clone()

        on_counter_TF = torch.zeros(num_thresholds, num_features).to(device)
        results[custom_function.__name__]["on_count"] = on_counter_TF
        results[custom_function.__name__]["off_count"] = on_counter_TF.clone()

    return results


def get_data_batch(
    data: dict[str, torch.Tensor],
    inputs_BL: list[str],
    start: int,
    end: int,
    custom_functions: list[callable],
) -> dict:
    """If the custom function returns a board of 8 x 8 x num_classes, we construct it on the fly.
    In this case, creating the state stack is very cheap compared to doing the statistics aggregation.
    Additionally, a full board state stack very quickly grows to dozens of gigabytes, so we don't want to store it.

    However, if the custom function returns a 1 x 1 x num_classes tensor, creating the state stack is comparable to the statistics aggregation.
    And memory usage is low, so it makes sense to compute the state stack once and store it."""
    batch_data = {}
    for custom_function in custom_functions:
        config = chess_utils.config_lookup[custom_function.__name__]
        if config.num_rows == 8:
            state_stacks = chess_utils.create_state_stacks(inputs_BL, custom_function).to(device)
            batch_data[custom_function.__name__] = chess_utils.state_stack_to_one_hot(
                config, device, state_stacks
            )
        else:
            batch_data[custom_function.__name__] = data[custom_function.__name__][start:end]

    return batch_data


def aggregate_batch_statistics(
    results: dict,
    custom_functions: list[callable],
    activations_FBL: torch.Tensor,
    thresholds_T111: torch.Tensor,
    batch_data: dict[str, torch.Tensor],
    f_start: int,
    f_end: int,
    f_batch_size: int,
) -> dict:
    """For every threshold for every activation for every feature, we check if it's above the threshold.
    If so, for every custom function we add the state stack (board or something like pin state) to the on_tracker.
    If not, we add it to the off_tracker.
    We also keep track of how many activations are above and below the threshold (on_count and off_count, respectively)
    This is done in parallel to make it fast."""
    active_indices_TFBL = activations_FBL > thresholds_T111

    active_counts_TF = einops.reduce(active_indices_TFBL, "T F B L -> T F", "sum")
    off_counts_TF = einops.reduce(~active_indices_TFBL, "T F B L -> T F", "sum")

    for custom_function in custom_functions:
        on_tracker_TFRRC = results[custom_function.__name__]["on"]
        off_tracker_FTRRC = results[custom_function.__name__]["off"]

        boards_BLRRC = batch_data[custom_function.__name__]
        boards_TFBLRRC = einops.repeat(
            boards_BLRRC,
            "B L R1 R2 C -> T F B L R1 R2 C",
            F=f_batch_size,
            T=len(thresholds_T111),
        )

        active_boards_sum_TFRRC = einops.reduce(
            boards_TFBLRRC * active_indices_TFBL[:, :, :, :, None, None, None],
            "T F B L R1 R2 C -> T F R1 R2 C",
            "sum",
        )
        off_boards_sum_TFRRC = einops.reduce(
            boards_TFBLRRC * ~active_indices_TFBL[:, :, :, :, None, None, None],
            "T F B L R1 R2 C -> T F R1 R2 C",
            "sum",
        )

        on_tracker_TFRRC[:, f_start:f_end, :, :, :] += active_boards_sum_TFRRC
        off_tracker_FTRRC[:, f_start:f_end, :, :, :] += off_boards_sum_TFRRC

        results[custom_function.__name__]["on"] = on_tracker_TFRRC
        results[custom_function.__name__]["off"] = off_tracker_FTRRC

        results[custom_function.__name__]["on_count"][:, f_start:f_end] += active_counts_TF
        results[custom_function.__name__]["off_count"][:, f_start:f_end] += off_counts_TF

    return results


def aggregate_statistics(
    custom_functions: list[callable],
    autoencoder_path: str,
    n_inputs: int,
    batch_size: int,
    device: str,
    model_path: str,
    data_path: str,
):

    torch.set_grad_enabled(False)
    feature_batch_size = batch_size

    with open(data_path, "rb") as f:
        data = pickle.load(f)

    for key in data:
        if key == "pgn_strings":
            continue
        data[key] = data[key].to(device)

    pgn_strings = data["pgn_strings"]
    del data["pgn_strings"]

    ae_bundle = get_ae_bundle(autoencoder_path, device, data, batch_size, model_path)
    ae_bundle.buffer = None

    features = torch.arange(0, ae_bundle.dictionary_size, device=device)
    num_features = len(features)

    assert len(pgn_strings) >= n_inputs
    assert n_inputs % batch_size == 0

    n_iters = n_inputs // batch_size
    num_feature_iters = num_features // feature_batch_size

    thresholds_T111 = (
        torch.arange(0.0, 1.0, 0.1).view(-1, 1, 1, 1).to(device)
    )  # Reshape for broadcasting

    results = initialize_results_dict(custom_functions, len(thresholds_T111))

    for i in tqdm(range(n_iters)):
        start = i * batch_size
        end = (i + 1) * batch_size
        inputs_BL = pgn_strings[start:end]

        batch_data = get_data_batch(data, inputs_BL, start, end, custom_functions)

        all_activations_FBL, encoded_inputs = collect_activations_batch(
            ae_bundle, inputs_BL, features
        )

        # For thousands of features, this would be many GB of memory. So, we minibatch.
        for feature in range(num_feature_iters):
            f_start = feature * feature_batch_size
            f_end = min((feature + 1) * feature_batch_size, num_features)
            f_batch_size = f_end - f_start

            activations_FBL = all_activations_FBL[
                f_start:f_end
            ]  # NOTE: Now F == feature_batch_size
            # Maybe that's stupid and inconsistent and I should use a new letter for annotations
            # I'll roll with it for now

            results = aggregate_batch_statistics(
                results,
                custom_functions,
                activations_FBL,
                thresholds_T111,
                batch_data,
                f_start,
                f_end,
                f_batch_size,
            )

    with open("results.pkl", "wb") as f:
        pickle.dump(results, f)


if __name__ == "__main__":
    custom_functions = [chess_utils.board_to_piece_state, chess_utils.board_to_pin_state]
    # custom_functions = [chess_utils.board_to_pin_state]
    autoencoder_path = "../autoencoders/group0/ef=4_lr=1e-03_l1=1e-01_layer=5/"
    batch_size = 10
    feature_batch_size = 10
    n_inputs = 100
    device = "cuda"
    # device = "cpu"
    model_path = "../models/"
    data_path = "data.pkl"

    aggregate_statistics(
        custom_functions, autoencoder_path, n_inputs, batch_size, device, model_path, data_path
    )
