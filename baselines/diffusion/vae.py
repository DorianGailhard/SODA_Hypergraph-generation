import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from torchvision.utils import save_image
from PIL import Image
import os
from pathlib import Path
import torchvision.transforms.functional as TF
from skimage.filters import threshold_local
import numpy as np
from torch.optim.lr_scheduler import CosineAnnealingLR

import hypernetx as hnx
import metrics
import pickle

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

class VAE(nn.Module):
    def __init__(self, latent_dim, img_shape):
        super(VAE, self).__init__()
        self.latent_dim = latent_dim
        self.img_shape = img_shape

        # Encoder
        self.encoder = nn.Sequential(
            nn.Linear(int(torch.prod(torch.tensor(img_shape))) + 1, 512),  # +1 for num_nodes
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.2, inplace=True),
        )
        
        self.fc_mu = nn.Linear(256, latent_dim)
        self.fc_var = nn.Linear(256, latent_dim)

        # Decoder
        self.decoder = nn.Sequential(
            nn.Linear(latent_dim + 1, 256),  # +1 for num_nodes
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(256, 512),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(512, int(torch.prod(torch.tensor(img_shape)))),
        )

    def encode(self, x, num_nodes):
        x = torch.cat([x, num_nodes], dim=1)
        x = self.encoder(x)
        mu = self.fc_mu(x)
        log_var = self.fc_var(x)
        return mu, log_var

    def reparameterize(self, mu, log_var):
        std = torch.exp(0.5 * log_var)
        eps = torch.randn_like(std)
        return mu + eps * std

    def decode(self, z, num_nodes):
        z = torch.cat([z, num_nodes], dim=1)
        return torch.sigmoid(self.decoder(z))

    def forward(self, x, num_nodes):
        x = x.view(x.size(0), -1)
        mu, log_var = self.encode(x, num_nodes)
        z = self.reparameterize(mu, log_var)
        return self.decode(z, num_nodes), mu, log_var

class ImageDataset(Dataset):
    def __init__(self, folder_path, dataset_name):
        self.folder_path = folder_path
        self.image_files = [f for f in os.listdir(folder_path) if f.lower().endswith(('.png', '.jpg', '.jpeg', '.bmp', '.tiff'))]
        
        self.transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Lambda(lambda x: x.clamp(0, 1))
        ])

        with open('../../datasets/' + dataset_name + '.pkl', 'rb') as file:
            dataset = pickle.load(file)
        self.n_nodes = [len(H.nodes) for H in dataset['train'] for _ in range(5)]

    def __len__(self):
        return len(self.image_files)

    def __getitem__(self, idx):
        img_name = os.path.join(self.folder_path, self.image_files[idx])
        image = Image.open(img_name)
        image = self.transform(image)
        num_nodes = torch.tensor([self.n_nodes[idx]], dtype=torch.float32)
        return image, num_nodes

def load_dataset(folder_name, dataset_name, batch_size=32):
    dataset = ImageDataset(folder_path=folder_name, dataset_name=dataset_name)
    return DataLoader(dataset, batch_size=batch_size, shuffle=True, num_workers=4, pin_memory=True)

def train_vae(folder_name, dataset_name, num_epochs=100, latent_dim=100, initial_lr=0.0002, model_save_path='vae_model.pth'):
    dataloader = load_dataset(folder_name, dataset_name)
    
    first_batch = next(iter(dataloader))
    img_shape = first_batch[0][0].shape
    
    vae = VAE(latent_dim, img_shape).to(device)
    
    optimizer = optim.Adam(vae.parameters(), lr=initial_lr)
    scheduler = optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=num_epochs)

    def loss_function(recon_x, x, mu, log_var):
        BCE = nn.functional.binary_cross_entropy(recon_x, x.view(-1, int(torch.prod(torch.tensor(x.shape[1:])))), reduction='sum')
        KLD = -0.5 * torch.sum(1 + log_var - mu.pow(2) - log_var.exp())
        return BCE + KLD

    for epoch in range(num_epochs):
        total_loss = 0
        for i, (imgs, num_nodes) in enumerate(dataloader):
            imgs = imgs.to(device)
            num_nodes = num_nodes.to(device)
            optimizer.zero_grad()
            
            recon_batch, mu, log_var = vae(imgs, num_nodes)
            loss = loss_function(recon_batch, imgs, mu, log_var)
            
            loss.backward()
            total_loss += loss.item()
            optimizer.step()

        scheduler.step()
        avg_loss = total_loss / len(dataloader.dataset)
        print(f'Epoch [{epoch + 1}/{num_epochs}], Average loss: {avg_loss:.4f}, LR: {scheduler.get_last_lr()[0]:.6f}')

    torch.save({
        'vae_state_dict': vae.state_dict(),
    }, model_save_path)

def adaptive_threshold(tensor, block_size=35, offset=10):
    np_image = tensor.squeeze().cpu().numpy()
    np_image = (np_image - np_image.min()) / (np_image.max() - np_image.min()) * 255
    np_image = np_image.astype(np.uint8)
    binary = np_image > 100
    binary_tensor = torch.from_numpy(binary.astype(np.float32)).unsqueeze(0)
    return binary_tensor

def sample_from_vae(model_path, num_samples, desired_num_nodes, latent_dim=100, img_shape=(1, 28, 28)):
    vae = VAE(latent_dim, img_shape).to(device)
    checkpoint = torch.load(model_path, map_location=device)
    vae.load_state_dict(checkpoint['vae_state_dict'])
    vae.eval()
    
    samples = []
    with torch.no_grad():
        for num_nodes in desired_num_nodes:
            z = torch.randn(1, latent_dim, device=device)
            num_nodes_tensor = torch.tensor([[num_nodes]], dtype=torch.float32).to(device)
            gen_img = vae.decode(z, num_nodes_tensor)
            samples.append(adaptive_threshold(gen_img.cpu().view(img_shape)))
    
    return samples

def train_all_datasets(base_data_folder, base_model_folder, num_epochs=100, latent_dim=100, initial_lr=0.0002):
    for dataset_folder in Path(base_data_folder).iterdir():
        if dataset_folder.is_dir():
            dataset_name = dataset_folder.name
            train_folder = dataset_folder / 'train'
            
            if train_folder.exists():
                model_save_path = os.path.join(base_model_folder, f'{dataset_name}_vae.pth')
                
                if os.path.exists(model_save_path):
                    print(f"Model for dataset {dataset_name} already exists. Skipping training.")
                    continue
                
                print(f"Training VAE for dataset: {dataset_name}")
                
                train_vae(
                    folder_name=str(train_folder),
                    dataset_name=dataset_name,
                    num_epochs=num_epochs,
                    latent_dim=latent_dim,
                    initial_lr=initial_lr,
                    model_save_path=model_save_path
                )
                print(f"VAE for {dataset_name} saved to {model_save_path}")
            else:
                print(f"Warning: Train folder not found for {dataset_name}")

def load_and_generate_samples(base_model_folder, base_data_folder, latent_dim=100):
    for model_path in Path(base_model_folder).glob('*vae.pth'):
        dataset_name = model_path.stem[:-4]  # Remove '_vae' from the name
        train_folder = Path(base_data_folder) / dataset_name / 'train'
        
        if not train_folder.exists():
            print(f"Warning: Train folder not found for {dataset_name}")
            continue
        
        # Load the dataset to get the desired number of nodes
        with open('../../datasets/' + dataset_name + '.pkl', 'rb') as file:
            dataset = pickle.load(file)
        desired_num_nodes = [len(H.nodes) for H in dataset['test']]
        
        first_image_path = next(train_folder.glob('*.*'), None)
        if first_image_path is None:
            print(f"Warning: No images found in train folder for {dataset_name}")
            continue
        
        with Image.open(first_image_path) as img:
            channels = len(img.getbands())
            width, height = img.size
        
        print(f"Processing dataset: {dataset_name}")
        print(f"Image dimensions: {channels}x{height}x{width}")
        
        generated_images = sample_from_vae(model_path, len(desired_num_nodes), desired_num_nodes, latent_dim, img_shape=(channels, height, width))
        
        output_folder = Path(base_data_folder) / dataset_name / 'generated_samples_vae'
        os.makedirs(output_folder, exist_ok=True)
        
        for i, img in enumerate(generated_images):
            save_image(img, str(output_folder / f"sample_{i+1}.png"), normalize=True)
        
        print(f"Generated samples saved in {output_folder}")

# Usage
base_data_folder = 'data'
base_model_folder = 'models'
train_all_datasets(base_data_folder, base_model_folder, num_epochs=1000, initial_lr=0.0002)
load_and_generate_samples(base_model_folder, base_data_folder)

# Define the base path to your dataset
base_path = "data"

# Define the validation metrics
validation_metrics = [
    metrics.NodeNumDiff(),
    metrics.NodeDegreeDistrWasserstein(),
    metrics.EdgeSizeDistrWasserstein(),
    metrics.Spectral(),
    metrics.Uniqueness(),
    metrics.Novelty(),
    metrics.CentralityCloseness(),
    metrics.CentralityBetweenness(),
    metrics.CentralityHarmonic(),
]

# Function to convert an incidence matrix image to a hypernetx hypergraph
def image_to_hypergraph(image_path, n_nodes):
    # Load image
    image = Image.open(image_path).convert('L')

    # Get original image dimensions
    original_width, original_height = image.size

    # Crop the image to maintain the original width but adjust the height
    cropped_image = image.crop((0, 0, original_width, n_nodes))

    # Convert to binary matrix
    matrix = np.array(cropped_image) // 255  # assuming black is 0 and white is 1
    rows, cols = np.where(matrix == 1)

    # Create hyperedges
    hyperedges = {}
    for i, j in zip(rows, cols):
        if j not in hyperedges:
            hyperedges[j] = []
        hyperedges[j].append(i)

    # Convert to hypernetx hypergraph
    hypergraph = hnx.Hypergraph(hyperedges)
    return hypergraph

for dataset_name in os.listdir(base_path):
    dataset_path = os.path.join(base_path, dataset_name)
    if os.path.isdir(dataset_path):
        # Add the ValidEgo metric if "hypergraphEgo" is in the dataset name
        current_metrics = validation_metrics.copy()
        
        if  "hypergraphEgo" in dataset_name:
            current_metrics.append(metrics.ValidEgo())
        
        if "hypergraphSBM" in dataset_name:
            current_metrics.append(metrics.ValidSBM())
        
        if "hypergraphTree" in dataset_name:
            current_metrics.append(metrics.ValidHypertree())
            
        # Load the dataset
        with open('../../data/' + dataset_name + '.pkl', 'rb') as file:
            dataset = pickle.load(file)

        # Collect all hypergraphs in the current dataset
        all_hypergraphs = []
        for i, test_hypergraph in enumerate(dataset['test']):
            n_nodes = len(test_hypergraph.nodes)
            hypergraph = image_to_hypergraph(dataset_path + '/generated_samples_vae/' + f'sample_{1+i}.png', n_nodes)
            all_hypergraphs.append(hypergraph)
        
        # Compute and print metrics
        print(f"Metrics for dataset {dataset_name}:")
        for metric in current_metrics:
            result = metric(dataset['test'], all_hypergraphs, dataset['train'])
            print(f"{metric}: {result}")
        print("\n" + "="*50 + "\n")