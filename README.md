# Reconstruction-Style

Reconstruction Style is a methodology involving the encoding of an image into tensors using typical image replication networks, then applying a VGG framework before the decoding.

### Adapting a new style
By using the pretrained reconstruction network, adapting a new style usually takes around 30 seconds on a Titain X with 200 iterations. On the same machine, arbitrary fast style transfer takes about 8 hours to produce similar results.

***THIS DOES NOT WORK ON WINDOWS (yet), due to issues with python on windows not being able to pickle lambadas for multithreading***

As a prerequisite, download and extract the [COCO Dataset](http://images.cocodataset.org/zips/train2017.zip)<br>
And install the requirements through pip: `pip install -r requirements.txt`
```
python src/main.py fast \
    --content-dataset [Path to COCO Dataset] \
    --style-image [Path to style image] \
    --model [Path to RCStyle.pth] \
    --save-model-dir [Path to save location] \
    --style-weight [Anything between 1e5 to 2.75e5 usually works, as long as loss is ~3.5] \
    --style-size [Set to style image height] \
    --layer [Use layer 1 or 0, they usually give the most style-heavy results] \
    --update-step [Between 200-500 steps] \
    --cuda 1
```
`Demonstration/Comparison images will be uploaded soon`

### Evaluating the saved model on images
```
python src/main.py test \
    --content-image [Path to content image] \
    --output-image [Path to output image] \
    --model [Location of model created when adapting a new style] \
    --content-size [Set to height of content image] \
    --original-colors [0 for no original colors, 1 for original colors] \
    --cuda 1
```
Note: Original colors requires that the content image and the output image be the same size.
`Demonstration/Comparison images will be uploaded soon`

### Evaluting the saved model on video
```
python src/main.py video \
    --content-video [Path to content video] \
    --output-video [Path to output video] \
    --model [Location of model created when adapting a new style] \
    --content-size [Set to height of content video] \
    --original-colors [0 for no original colors, 1 for original colors] \
    --cuda 1
```
Note: Original colors requires that the content video and the output video be the same size.
`Demonstration/Comparison images will be uploaded soon`

### Training a new reconstruction network
The pretrained model was trained on the default parameters, and used the [COCO Dataset](http://images.cocodataset.org/zips/train2017.zip) for content images, and [WikiArt](https://www.kaggle.com/c/painter-by-numbers/data) for style images.
```
python src/main.py train \
    --content-dataset [Path to COCO Dataset] \
    --style-dataset [Path to style dataset] \
    --cuda 1
```
The training process can be monitored using Tensorboard. Since bilevel optimization requires "second-order" gradient computation, the training process might take a long time (Approaching 20 hours of training on a Titain X). The GPU memory consumption is massive.
