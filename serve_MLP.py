import wandb
import yaml

if __name__ == '__main__':
    with open("configs-cora-gcn.yaml", "r") as f:
        cfg = yaml.load(f, Loader=yaml.FullLoader)
    dbs = cfg['wandb']
    print(dbs)
    total = 1
    for k, vs in dbs['parameters'].items():
        total *= len(vs['values'])
    print(f"total: {total}")
    # sweep_id = wandb.sweep(sweep=dbs, project='CAGCN-DropEdge-search')
