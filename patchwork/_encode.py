import numpy as np
from PIL import Image
import tensorflow as tf

from patchwork._layers import ChannelWiseDense
from tensorflow.keras.preprocessing.image import ImageDataGenerator

def build_encoder(num_channels=3):
    """
    Inpainting encoder model from Pathak et al
    """
    inpt = tf.keras.layers.Input((None, None, num_channels))
    net = inpt
    for k in [64, 64, 128, 256, 512]:
        net = tf.keras.layers.Conv2D(k, 4, strides=2, padding="same")(net)
        net = tf.keras.layers.LeakyReLU(alpha=0.2)(net)
        net = tf.keras.layers.BatchNormalization()(net)
    return tf.keras.Model(inpt, net, name="encoder")


def build_decoder(input_channels=512, num_channels=3):
    """
    Inpainting decoder from Pathak et al
    """
    inpt = tf.keras.layers.Input((None, None, input_channels))
    net = inpt

    for k in [512, 256, 128, 64, 64]:
        net = tf.keras.layers.Conv2DTranspose(k, 4, strides=2, 
                                              padding="same",
                                activation=tf.keras.activations.relu)(net)
        net = tf.keras.layers.BatchNormalization()(net)
    
    net = tf.keras.layers.Conv2D(num_channels, 3, strides=1, padding="same", 
                             activation=tf.keras.activations.sigmoid)(net)
    return tf.keras.Model(inpt, net, name="decoder")


def build_discriminator(num_channels=3):
    """
    Inpainting discriminator from Pathak et al
    """
    inpt = tf.keras.layers.Input((None, None, num_channels))
    net = inpt
    for k in [64, 128, 256, 512]:
        net = tf.keras.layers.Conv2D(k, 4, strides=2, padding="same",
                                activation=tf.keras.activations.relu)(net)
        net = tf.keras.layers.BatchNormalization()(net)
    net = tf.keras.layers.GlobalMaxPool2D()(net)
    net = tf.keras.layers.Dense(1, activation=tf.keras.activations.sigmoid,
                               name="disc_pred")(net)
    return tf.keras.Model(inpt, net, name="discriminator")





def build_inpainting_network(input_shape=(256,256,3), disc_loss=0.001, 
                             learn_rate=1e-4, encoder=None, 
                             decoder=None, discriminator=None):
    """
    Build an inpaainting network as described in the supplementary 
    material of Pathak et al's paper.
    
    :input_shape: 3-tuple giving the shape of images to be inpainted
    :disc_loss: weight for the discriminator component of the loss function.
        1-disc_loss will be applied to the reconstruction loss
    :learn_rate: learning rate for inpainter. discriminator will be set to
        1/10th of this
    :encoder: encoder model (if not specified one will be built)
    :decoder: decoder model
    :discriminator: discriminator model
    
    Returns inpainter, encoder, and discriminator models.
    """
    # initialize encoder and decoder objects
    if encoder is None: 
        encoder = build_encoder(input_shape[-1])
    if decoder is None:
        decoder = build_decoder(num_channels=input_shape[-1])

    inpt = tf.keras.layers.Input(input_shape, name="inpt")
    
    # Pathak's structure runs images through the encoder, then a dense
    # channel-wise layer, then dropout and a 1x1 Convolution before decoding.
    encoded = encoder(inpt)
    dense = ChannelWiseDense()(encoded)
    dropout = tf.keras.layers.Dropout(0.5)(dense)
    conv1d = tf.keras.layers.Conv2D(512,1)(dropout)
    decoded = decoder(conv1d)
    
    # NOW FOR THE ADVERSARIAL PART
    if discriminator is None:
        discriminator = build_discriminator(input_shape[-1])
    discriminator.compile(tf.keras.optimizers.Adam(0.1*learn_rate), 
                          loss=tf.keras.losses.binary_crossentropy)
    discriminator.trainable = False
    disc_pred = discriminator(decoded)
    

    inpainter = tf.keras.Model(inpt, [decoded, disc_pred])
    inpainter.compile(tf.keras.optimizers.Adam(learn_rate),
                      loss={"decoder":tf.keras.losses.mse,
                            "discriminator":tf.keras.losses.binary_crossentropy},
                            loss_weights={"decoder":1-disc_loss, 
                                          "discriminator":disc_loss})
    return inpainter, encoder, discriminator




def build_context_encoder(disc_loss=0.001):
    """
    Build a context encoder, mostly following Pathak et al. FOR NOW assumes images
    are (256,256,3).
    
    Returns the context encoder as well as an encoder model.
    """
    # initialize encoder and decoder objects
    encoder = build_encoder()
    decoder = build_decoder()
    # inputs for this model: the image and a mask (which we'll use to only
    # count loss from the masked area)
    inpt = tf.keras.layers.Input((256,256,3), name="inpt")
    inpt_mask = tf.keras.layers.Input((256,256,3), name="inpt_mask")
    # Pathak's structure runs images through the encoder, then a dense
    # channel-wise layer, then a 1x1 Convolution before decoding.
    encoded = encoder(inpt)
    updated = ChannelWiseDense()(encoded)
    dropout = tf.keras.layers.Dropout(0.5)(updated)
    conv1d = tf.keras.layers.Conv2D(512,1)(dropout)
    decoded = decoder(conv1d)
    # create a masked output to compare with ground truth (which should
    # already have it's unmasked areas set to 0)
    masked_decoded = tf.keras.layers.Multiply(name="masked_decoded")(
                                    [inpt_mask, decoded])
    
    # NOW FOR THE ADVERSARIAL PART
    discriminator = build_discriminator()
    discriminator.compile(tf.keras.optimizers.Adam(1e-5), 
                          loss=tf.keras.losses.binary_crossentropy)
    discriminator.trainable = False
    disc_pred = discriminator(decoded)
    

    context_encoder = tf.keras.Model([inpt, inpt_mask], 
                                     [decoded, masked_decoded, disc_pred])
    context_encoder.compile(tf.keras.optimizers.Adam(1e-4),
                            loss={"masked_decoded":tf.keras.losses.mse,
                                 "discriminator":tf.keras.losses.binary_crossentropy},
                        #loss_weights={"masked_decoded":1.0, "discriminator":0.0})
                        loss_weights={"masked_decoded":1-disc_loss, "discriminator":disc_loss})
                           #loss_weights={"masked_decoded":0.999, "discriminator":0.001})
                           

    
    return context_encoder, encoder, discriminator

def mask_generator(H,W,C):
    """
    Generates random rectangular masks
    """
    a = int(0.05*(H+W)/2)
    b = int(0.45*(H+W)/2)
    while True:
        mask = np.zeros((H,W,C), dtype=bool)
        xmin = np.random.randint(a,b)
        ymin = np.random.randint(a,b)
        xmax = np.random.randint(W-b, W-a)
        ymax = np.random.randint(H-b, H-a)
        mask[ymin:ymax, xmin:xmax,:] = True
        yield mask

def make_test_mask(H,W,C):
    """
    Generate a mask for a (H,W,C) image that crops out the center fourth.
    """
    mask = np.zeros((H,W,C), dtype=bool)
    mask[int(0.25*H):int(0.75*H), int(0.25*W):int(0.75*W),:] = True
    return mask


def train_and_test(filepaths, imsize=(256,256), channels=3, norm=255, batch_size=64):
    """
    Load a set of images into memory from file, split 90-10 into train/test
    sets, and build a generator that will do augmentation on the training set.
    
    :filepaths: list of strings pointing to image files (FOR NOW assuming all
        are 256x256x3
    
    Returns
    :train_generator: function to return a generator for keras.Model.fit_generator()
    :val_data: nested tuples of the form ((x, mask),y) to pass to the validation_data
        kwarg of keras.Model.fit_generator()
    
    """
    shape = (imsize[0], imsize[1], channels)
    #N = 256
    #mask_start = int(N/4)
    #mask = np.zeros((N,N,3), dtype=bool)
    #mask[mask_start:3*mask_start, mask_start:3*mask_start,:] = True
    
    all_files = [x.strip() for x in open(filepaths, "r").readlines()]
    all_ims = np.stack([np.array(Image.open(x).resize(imsize)) 
                        for x in all_files])/norm
    test_ims = all_ims[np.arange(all_ims.shape[0]) % 10 == 0,:,:,:]
    train_ims = all_ims[np.arange(all_ims.shape[0]) % 10 != 0,:,:,:]

    test_mask = make_test_mask(*shape)
    train = train_ims
    test_y = test_ims.copy() 
    test_y[:,~test_mask] = 0

    test_x = test_ims.copy()
    test_x[:,test_mask] = 0
    
    def train_generator(batch_size=batch_size):
        datagen = ImageDataGenerator(rotation_range=10, 
                            width_shift_range=0.05,
                            height_shift_range=0.05,
                            zoom_range=0.2,
                            horizontal_flip=True,
                            vertical_flip=True,
                            data_format="channels_last",
                            brightness_range=[0.8,1.2])

        _gen = datagen.flow(train, batch_size=batch_size)
        maskgen = mask_generator(*shape)
        while True:
            aug_imgs = next(_gen)/norm
            #masks = np.stack([mask]*aug_imgs.shape[0])
            masks = np.stack([next(maskgen) for _ in range(aug_imgs.shape[0])])
            x = aug_imgs.copy()
            x[masks] = 0
            y = aug_imgs.copy()
            y[~masks] = 0
            yield ((x, masks), y)
    
    #return train_x, train_y, test_x, test_y, mask # added mask
    return train_generator, ((test_x, np.stack([test_mask]*test_x.shape[0])), test_y)#, mask # added mask