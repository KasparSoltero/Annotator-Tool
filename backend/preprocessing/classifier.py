# Pipeline implementation for integration with the annotation tool
import torch
import torchvision
import torch.nn as nn
import torch.optim as optim
from torch.optim import lr_scheduler
from torch.utils.data import Dataset, DataLoader
import pandas as pd
import torchaudio
import torchvision.transforms as transforms
import os
import numpy as np
import matplotlib.pyplot as plt
from transformers import AutoFeatureExtractor, ASTForAudioClassification
import random

class UserModel(nn.Module):
    # Create user model for transfer learning
    def __init__(self, classes):
        super(UserModel, self).__init__()
        in_features = 527
        self.linear1 = torch.nn.Linear(in_features, 100)
        self.activation = torch.nn.ReLU()
        self.linear2 = torch.nn.Linear(100, len(classes)) # This needs to be dynamically set
        self.softmax = torch.nn.Softmax(dim=1)

    def forward(self, x):
        x = self.linear1(x)
        x = self.activation(x)
        x = self.linear2(x)
        x = self.softmax(x)
        return x

class AudioDataset(Dataset):
    # ANNOTATIONS, AUDIO_DIR, mel_spectrogram, 16000, False
    def __init__(self, annotations_file, audio_dir, sr, val):
        super(AudioDataset, self).__init__()
        self.val = val
        self.annotations = self._filter_annotations(pd.read_csv(annotations_file))
        self.audio_dir = audio_dir
        self.target_rate = sr
        self.resize = transforms.Resize((224,224))
        self.feature_extractor = AutoFeatureExtractor.from_pretrained("MIT/ast-finetuned-audioset-10-10-0.4593")

    def __len__(self):
        return len(self.annotations)

    def __getitem__(self, index):
        audio_path = self._get_audio_path(index)
        label = self._get_audio_label(index)
        signal, sr = torchaudio.load(audio_path)
        resample = torchaudio.transforms.Resample(sr, self.target_rate)
        signal = resample(signal[0])
        feature = self.feature_extractor(signal, sampling_rate=self.target_rate, return_tensors="pt")
        feature = feature['input_values']
        return feature,label

    def _get_audio_path(self, index):
        path = os.path.join(self.audio_dir, self.annotations.iloc[index,1])
        return path

    def _get_audio_label(self, index):
        return self.annotations.iloc[index,3]

    def _get_validation(self, index):
        return self.annotations.iloc[index,2]

    def _filter_annotations(self, data):
        return data.loc[data['Validation'] == self.val]
    
    def shuffle(self):
        pass


def encode(labels, classes):
    target = []
    for label in labels:
        y = [0 for i in range(len(classes))] # This needs to be dynamically set
        y[classes.index(label)] = 1
        target.append(y)
    target = torch.Tensor(target)
    return target


def train(training_set, validation_set, classes, num_epoch=2, batch_size=12):
    device = (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    usermodel = UserModel(classes)
    usermodel = usermodel.to(device)

    # load model
    MODEL_PATH = "./preprocessing/model/model.pth"
    if not os.path.exists(MODEL_PATH):
        print('Generating model (train)')
        os.mkdir(f'./preprocessing/model/')
        model = ASTForAudioClassification.from_pretrained("MIT/ast-finetuned-audioset-10-10-0.4593")
    else:
        model = torch.load(MODEL_PATH)
    model = model.to(device)

    criterion = nn.CrossEntropyLoss()

    train_loss = []
    train_accuracy = []
    val_loss = []
    val_accuracy = [] 
    best_acc = 0

    learning_rate = 1e-6

    # TODO augment data using noise
    # augmenting_noise, sr = torchaudio.load('./preprocessing/augmenting_noise.wav')
    # resample = torchaudio.transforms.Resample(sr, 16000)
    # augmenting_noise = resample(augmenting_noise[0])
    # print(f'augmenting_noise looks like {augmenting_noise.shape} {augmenting_noise.dtype}')

    print(f'Number of training samples: {len(training_set)}')
    # Recommended hyper-parameters - epoch:25, lr:1e-5 (halving every 5 epochs after epoch 10), batch:12
    for epoch in range(num_epoch):
        optimizer = optim.Adam(list(model.parameters()) + list(usermodel.parameters()), lr=learning_rate) # Removed model.parameters()

        if epoch > 2:
            learning_rate = learning_rate/2

        running_loss = 0
        running_corrects = 0
        val_running_loss = 0
        corrects = 0
        total = 0
        i = 0

        GT = []
        pred = []

        # augment data with time translation
        num_augmented = 4
        time_shift_ms = 10
        time_shift_samples = int((time_shift_ms * 16000) / 1000)
        print(f'time shift samples: {time_shift_samples}')

        for data_original, label in training_set:
            print(f'data looks like {data_original.shape} {data_original.dtype}')

            # Augment data
            for aug_index in range(num_augmented):
                time_shift = random.randint(-time_shift_samples, time_shift_samples)
                data_aug = torch.roll(data_original, time_shift, dims=-2)

                print(f'{aug_index} data_aug looks like {data_aug.shape} {data_aug.dtype} with time shift {time_shift}')
                # flattened_data = data_aug.view(1, -1)

                # # save augmented data
                # torchaudio.save('./static/test.wav', data.view(1, -1), 16000)
                # torchaudio.save(f'./static/test_{i}.wav', flattened_data, 16000)

                data = data_aug.to(device)
                data = torch.squeeze(data,1)
                optimizer.zero_grad()

                embeddings = model(data).logits
                outputs = usermodel(embeddings)
                y = encode([label], classes) # list wrapper as label not batched
                y = y.to(device)

                loss = criterion(outputs, y)
                loss.backward()
                optimizer.step()

                total += len(torch.argmax(y,dim=1))
                corrects += (torch.argmax(y,dim=1) == torch.argmax(outputs,dim=1)).sum()
                running_corrects = 100*corrects/total

                accuracy = running_corrects.item()
                running_loss += loss.item()
                
                if (i % 100 == 1) and (i != 1):
                    print(f"[{i}/{len(training_set)}] - Training Accuracy: {accuracy:.2f}, Training loss: {running_loss/i:.2f}")
                i += 1

        train_loss.append(running_loss)
        train_accuracy.append(accuracy)

        val_running = 0
        val_total = 0
        val_corrects = 0
        i = 0

        for data, label in validation_set:
            data = data.to(device)
            data = torch.squeeze(data,1)

            embeddings = model(data).logits
            outputs = usermodel(embeddings)

            y = encode([label], classes)
            y = y.to(device)
            loss = criterion(outputs, y)

            val_total += len(torch.argmax(y,dim=1))
            val_corrects += (torch.argmax(y,dim=1) == torch.argmax(outputs,dim=1)).sum()
            val_running = 100*val_corrects/val_total
            val_running_accuracy = val_running.item()

            val_running_loss += loss.item()

            GT.append(list(y[0].cpu()).index(1))
            pred.append(torch.argmax(outputs,dim=1).cpu().item())

            if (i % 100 == 1) and (i != 1):
                print(f"Val Accuracy: {val_running_accuracy:.2f}, Val loss: {val_running_loss/i:.2f}")
            i += 1

        val_loss.append(val_running_loss)
        val_accuracy.append(val_running_accuracy)
        
        print(f'[{epoch + 1}], Training Accuracy: {accuracy:.2f}, Training loss: {running_loss/len(training_set):.2f}, Val Accuracy: {val_running_accuracy:.2f}, Val loss: {val_running_loss/len(validation_set):.2f}')

        if (val_running_accuracy > best_acc):
            best_acc = val_running_accuracy
            print('Saving models')
            # torch.save(enhance, "./models/enhance.pth")
            torch.save(model, "./preprocessing/model/model.pth")
            torch.save(usermodel, "./preprocessing/model/usermodel.pth")
        
    return train_loss, train_accuracy, val_loss, val_accuracy

def predict(filename, user, classes, target_rate=16000):
    device = (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    PATH = f'./static/{user.id}/seg/{filename}'
    signal, sr = torchaudio.load(PATH)
    resample = torchaudio.transforms.Resample(sr, target_rate)
    signal = resample(signal[0])
    feature_extractor = AutoFeatureExtractor.from_pretrained("MIT/ast-finetuned-audioset-10-10-0.4593")
    feature = feature_extractor(signal, sampling_rate=target_rate, return_tensors="pt")
    feature = feature['input_values'].to(device)

    MODEL_PATH = f'./preprocessing/model/model.pth'

    # Load trained models
    if os.path.exists(MODEL_PATH):
        model = torch.load("./preprocessing/model/model.pth").to(device)
        usermodel = torch.load("./preprocessing/model/usermodel.pth").to(device)

        embeddings = model(feature).logits
        outputs = usermodel(embeddings)

        index = torch.argmax(outputs,dim=1).item()
        prediction = classes[index]
        confidence = outputs[0][index].item()

        return prediction, confidence
    else:
        return 'unknown', 0
    
def embeddings(filename, user, target_rate=16000):
    # Output embeddings of feature extractor block
    print(f'Generating embeddings for {filename}')
    device = (
        "cuda:0"
        if torch.cuda.is_available()
        else "mps"
        if torch.backends.mps.is_available()
        else "cpu"
    )
    PATH = f'./static/{user.id}/seg/{filename}'
    signal, sr = torchaudio.load(PATH)
    resample = torchaudio.transforms.Resample(sr, target_rate)
    signal = resample(signal[0])
    feature_extractor = AutoFeatureExtractor.from_pretrained("MIT/ast-finetuned-audioset-10-10-0.4593")
    feature = feature_extractor(signal, sampling_rate=target_rate, return_tensors="pt")
    feature = feature['input_values'].to(device)

    # Load trained models if they exist else pretrained
    MODEL_PATH = "./preprocessing/model/model.pth"
    if not os.path.exists(MODEL_PATH):
        print('Generating model (embeddings)')
        model = ASTForAudioClassification.from_pretrained("MIT/ast-finetuned-audioset-10-10-0.4593")
    else:
        model = torch.load(MODEL_PATH)
    model = model.to(device)

    embeddings = model(feature).logits.cpu().detach().numpy()
    return embeddings

def pipeline(user, classes):
    AUDIO_DIR = f"./static/{user.id}/seg/"
    ANNOTATIONS = f"./classifier/{user.id}/model/annotations.csv"

    training = AudioDataset(ANNOTATIONS, AUDIO_DIR, 16000, False)
    validation = AudioDataset(ANNOTATIONS, AUDIO_DIR, 16000, True)
    
    print('training... (pipeline)')
    loss, acc, val_loss, val_acc = train(training, validation, classes)
    print('done training (pipeline)')

    return loss, acc, val_loss, val_acc