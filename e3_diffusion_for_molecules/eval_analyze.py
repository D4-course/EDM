# Rdkit import should be first, do not move it
try:
    from rdkit import Chem
except ModuleNotFoundError:
    pass
import utils
import argparse
from qm9 import dataset
from qm9.models import get_model
import os
from equivariant_diffusion.utils import assert_mean_zero_with_mask, remove_mean_with_mask,\
    assert_correctly_masked
import torch
import time
import pickle
from configs.datasets_config import get_dataset_info
from os.path import join
from qm9.sampling import sample
from qm9.analyze import analyze_stability_for_molecules, analyze_node_distribution
from qm9.utils import prepare_context, compute_mean_mad
from qm9 import visualizer as qm9_visualizer
import qm9.losses as losses

try:
    from qm9 import rdkit_functions
except ModuleNotFoundError:
    print('Not importing rdkit functions.')


def check_mask_correct(variables, node_mask):
    for variable in variables:
        assert_correctly_masked(variable, node_mask)


def analyze_and_save(args, eval_args, device, generative_model,
                     nodes_dist, prop_dist, dataset_info, n_samples=10,
                     batch_size=10, save_to_xyz=False):
    batch_size = min(batch_size, n_samples)
    assert n_samples % batch_size == 0
    molecules = {'one_hot': [], 'x': [], 'node_mask': []}
    start_time = time.time()
    for i in range(int(n_samples/batch_size)):
        print(f"{i} / {int(n_samples/batch_size)}")
        nodesxsample = nodes_dist.sample(batch_size)
        one_hot, charges, x, node_mask = sample(
            args, device, generative_model, dataset_info, prop_dist=prop_dist, nodesxsample=nodesxsample)

        molecules['one_hot'].append(one_hot.detach().cpu())
        molecules['x'].append(x.detach().cpu())
        molecules['node_mask'].append(node_mask.detach().cpu())

        current_num_samples = (i+1) * batch_size
        secs_per_sample = (time.time() - start_time) / current_num_samples
        print('\t %d/%d Molecules generated at %.2f secs/sample' % (
            current_num_samples, n_samples, secs_per_sample))

        if save_to_xyz:
            id_from = i * batch_size
            qm9_visualizer.save_xyz_file(
                join(eval_args.model_path, 'eval/analyzed_molecules/'),
                one_hot, charges, x, dataset_info, id_from, name='molecule',
                node_mask=node_mask)

    from qm9.rdkit_functions import build_molecule
    from rdkit import Chem
    from rdkit.Chem import AllChem

    all_smiles = list()

    for batch in range(len(molecules['x'])):
        for each_mol in range(len(molecules['x'][batch])):
            positions = molecules['x'][batch][each_mol]
            atom_type = torch.argmax(molecules['one_hot'][batch][each_mol], dim=1).numpy()
            mol_objs = build_molecule(positions, atom_type, dataset_info)

            smiles_str = Chem.MolToSmiles(mol_objs)

            import re
            smiles_str = re.sub(r'%[0-9]*', '', smiles_str)

            all_smiles.append(smiles_str)

    return None, all_smiles

    molecules = {key: torch.cat(molecules[key], dim=0) for key in molecules}
    stability_dict, rdkit_metrics = analyze_stability_for_molecules(
        molecules, dataset_info)

    return stability_dict, rdkit_metrics


def test(args, flow_dp, nodes_dist, device, dtype, loader, partition='Test', num_passes=1):
    flow_dp.eval()
    nll_epoch = 0
    n_samples = 0
    for pass_number in range(num_passes):
        with torch.no_grad():
            for i, data in enumerate(loader):
                # Get data
                x = data['positions'].to(device, dtype)
                node_mask = data['atom_mask'].to(device, dtype).unsqueeze(2)
                edge_mask = data['edge_mask'].to(device, dtype)
                one_hot = data['one_hot'].to(device, dtype)
                charges = (data['charges'] if args.include_charges else torch.zeros(0)).to(device, dtype)

                batch_size = x.size(0)

                x = remove_mean_with_mask(x, node_mask)
                check_mask_correct([x, one_hot], node_mask)
                assert_mean_zero_with_mask(x, node_mask)

                h = {'categorical': one_hot, 'integer': charges}

                if len(args.conditioning) > 0:
                    context = prepare_context(args.conditioning, data).to(device, dtype)
                    assert_correctly_masked(context, node_mask)
                else:
                    context = None

                # transform batch through flow
                nll, _, _ = losses.compute_loss_and_nll(args, flow_dp, nodes_dist, x, h, node_mask,
                                                        edge_mask, context)
                # standard nll from forward KL

                nll_epoch += nll.item() * batch_size
                n_samples += batch_size
                if i % args.n_report_steps == 0:
                    print(f"\r {partition} NLL \t, iter: {i}/{len(loader)}, "
                          f"NLL: {nll_epoch/n_samples:.2f}")

    return nll_epoch/n_samples


def main(num_samples=-1):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model_path', type=str, default="outputs/edm_1",
                        help='Specify model path')
    parser.add_argument('--n_samples', type=int, default=100,
                        help='Specify model path')
    parser.add_argument('--batch_size_gen', type=int, default=100,
                        help='Specify model path')
    parser.add_argument('--save_to_xyz', type=eval, default=False,
                        help='Should save samples to xyz files.')

    eval_args, unparsed_args = parser.parse_known_args()

    assert eval_args.model_path is not None

    if num_samples != -1:
        eval_args.n_samples = num_samples
        eval_args.model_path = "outputs/edm_qm9"

    with open(join(eval_args.model_path, 'args.pickle'), 'rb') as f:
        args = pickle.load(f)

    # CAREFUL with this -->
    if not hasattr(args, 'normalization_factor'):
        args.normalization_factor = 1
    if not hasattr(args, 'aggregation_method'):
        args.aggregation_method = 'sum'

    args.cuda = not args.no_cuda and torch.cuda.is_available()
    device = torch.device("cuda" if args.cuda else "cpu")
    args.device = device
    dtype = torch.float32
    utils.create_folders(args)

    # Retrieve QM9 dataloaders
    dataloaders, charge_scale = dataset.retrieve_dataloaders(args)

    dataset_info = get_dataset_info(args.dataset, args.remove_h)

    # Load model
    generative_model, nodes_dist, prop_dist = get_model(args, device, dataset_info, dataloaders['train'])
    if prop_dist is not None:
        property_norms = compute_mean_mad(dataloaders, args.conditioning, args.dataset)
        prop_dist.set_normalizer(property_norms)
    generative_model.to(device)

    fn = 'generative_model_ema.npy' if args.ema_decay > 0 else 'generative_model.npy'
    flow_state_dict = torch.load(join(eval_args.model_path, fn), map_location=device)
    generative_model.load_state_dict(flow_state_dict)

    # Analyze stability, validity, uniqueness and novelty
    stability_dict, rdkit_metrics = analyze_and_save(
        args, eval_args, device, generative_model, nodes_dist,
        prop_dist, dataset_info, n_samples=eval_args.n_samples,
        batch_size=eval_args.batch_size_gen, save_to_xyz=eval_args.save_to_xyz)
    print(stability_dict)
    if num_samples != -1:
        return rdkit_metrics

    if rdkit_metrics is not None:
        rdkit_metrics = rdkit_metrics[0]
        print("Validity %.4f, Uniqueness: %.4f, Novelty: %.4f" % (rdkit_metrics[0], rdkit_metrics[1], rdkit_metrics[2]))
    else:
        print("Install rdkit roolkit to obtain Validity, Uniqueness, Novelty")

    # In GEOM-Drugs the validation partition is named 'val', not 'valid'.
    if args.dataset == 'geom':
        val_name = 'val'
        num_passes = 1
    else:
        val_name = 'valid'
        num_passes = 5

    # Evaluate negative log-likelihood for the validation and test partitions
    val_nll = test(args, generative_model, nodes_dist, device, dtype,
                   dataloaders[val_name],
                   partition='Val')
    print(f'Final val nll {val_nll}')
    test_nll = test(args, generative_model, nodes_dist, device, dtype,
                    dataloaders['test'],
                    partition='Test', num_passes=num_passes)
    print(f'Final test nll {test_nll}')

    print(f'Overview: val nll {val_nll} test nll {test_nll}', stability_dict)
    with open(join(eval_args.model_path, 'eval_log.txt'), 'w') as f:
        print(f'Overview: val nll {val_nll} test nll {test_nll}',
              stability_dict,
              file=f)


if __name__ == "__main__":
    main()
