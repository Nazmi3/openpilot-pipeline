import onnx
from onnx2pytorch import ConvertModel
import torch
import onnxruntime as rt
import os

parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGINAL_MODEL = os.path.join(parent_dir, 'common/models/supercombo.onnx')


def reinitialize_weights(layer_weight):
    torch.nn.init.xavier_uniform_(layer_weight)


def load_trainable_model(path_to_supercombo, trainable_layers=[], reinit_head=False):
    """Load supercombo as a trainable torch model.

    `reinit_head=False` (default) WARM-STARTS the trainable layers from the
    teacher's own pretrained weights, so distillation only has to preserve
    the mapping under a frozen backbone rather than rediscover it. The
    trainable layers here are ~1.67M params (11.8% of the model) and the
    dataset is under an hour, so re-learning them from scratch is the
    dominant source of student/teacher divergence.

    `reinit_head=True` restores the previous behaviour (xavier re-init +
    bias 0.01), i.e. train the path-plan head from scratch.
    """

    onnx_model = onnx.load(path_to_supercombo)
    model = ConvertModel(onnx_model, experimental=True)  # pretrained_model

    # enable batch_size > 1 for onnx2pytorch
    model.Constant_1047.constant[0] = -1
    model.Constant_1049.constant[0] = -1
    model.Constant_1051.constant[0] = -1
    model.Constant_1053.constant[0] = -1
    model.Constant_1057.constant[0] = -1
    model.Constant_1059.constant[0] = -1

    # ensure immutability https://github.com/ToriML/onnx2pytorch/pull/38
    model.Elu_907.inplace = False

    # optionally reinitialize trainable layers (default: keep pretrained weights)
    if reinit_head:
        n_reinit = 0
        for layer_name, layer in model.named_children():
            # TODO: support layers other than Linear?
            if isinstance(layer, torch.nn.Linear) and layer_name in trainable_layers:
                reinitialize_weights(layer.weight)
                layer.bias.data.fill_(0.01)
                n_reinit += 1
        print(f'=> re-initialized {n_reinit}/{len(trainable_layers)} trainable layers (from scratch)')
    elif trainable_layers:
        print(f'=> warm-starting {len(trainable_layers)} trainable layers from pretrained weights')

    # freeze other layers
    n_train_params = 0
    n_total_params = 0
    for name, param in model.named_parameters():
        name_layer = name.split(".")[0]
        n_total_params += param.numel()
        if name_layer in trainable_layers:
            param.requires_grad = True
            n_train_params += param.numel()
        else:
            param.requires_grad = False

    if trainable_layers:
        # Guard: layer names are onnx2pytorch module names ({op}_{output_tensor}),
        # NOT the raw ONNX node names. A mismatch silently freezes everything and
        # makes training a no-op, so fail loudly instead.
        if n_train_params == 0:
            raise RuntimeError(
                f'None of trainable_layers={trainable_layers} matched any module in the '
                f'converted model -- every parameter is frozen and training would be a '
                f'no-op. These must be onnx2pytorch names, e.g. "Gemm_1036" (= raw ONNX '
                f'node Gemm_328, named after its output tensor).')
        print(f'=> trainable: {n_train_params:,} / {n_total_params:,} params '
              f'({100*n_train_params/n_total_params:.1f}%) in {trainable_layers}')

    return model


def load_inference_model(path_to_model):
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if path_to_model.endswith('.onnx'):
        onnx_graph = onnx.load(path_to_model)
        output_names = [node.name for node in onnx_graph.graph.output]
        model = rt.InferenceSession(path_to_model, providers=['CPUExecutionProvider'])

        def run_model(inputs):
            outs =  model.run(output_names, inputs)[0]
            recurrent_state = outs[:, -512:]
            return outs, recurrent_state


    elif path_to_model.endswith('.pth'):

        model = load_trainable_model(ORIGINAL_MODEL)
        # map_location=device so a checkpoint saved on GPU still loads on
        # CPU-only hosts (the test-video launcher runs inference on CPU to
        # sidestep GPU-arch/torch-version compat issues on newer hardware).
        model.load_state_dict(torch.load(path_to_model, map_location=device))
        model.eval()
        model = model.to(device)

        def run_model(inputs):
            with torch.no_grad():
                inputs = {k: torch.from_numpy(v).to(device) for k, v in inputs.items()}
                outs = model(**inputs).cpu().numpy()
                # return numpy recurrent state, consistent with the .onnx branch,
                # so callers can refeed it directly as the next 'initial_state'
                recurrent_state = outs[:, -512:]
                return outs, recurrent_state

    return model, run_model


if __name__ == "__main__":
    pathplan_layer_names  = ["Gemm_959", "Gemm_981","Gemm_983","Gemm_1036"]
    path_to_supercombo = '../common/models/supercombo.onnx'
    model = load_trainable_model(pathplan_layer_names, path_to_supercombo)
