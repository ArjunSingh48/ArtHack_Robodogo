# Photo stand-ins

Rehearsal / no-camera stand-ins for the participant (`--source photo:<posture>`, and the
"Start with a sample photo" button). The real pose estimator reads them, so the whole pipeline
(pose → painting match → 3-D avatar) runs as with a person in front of the camera.

All from [Unsplash](https://unsplash.com), [Unsplash License](https://unsplash.com/license) (free to use, no permission needed).

| File | Photo | Photographer |
| --- | --- | --- |
| sitting.jpg | https://unsplash.com/photos/EJ08zy1a-UI | @davidprspctive |
| standing.jpg | https://unsplash.com/photos/ymo_yC_N_2o | @mitchel3uo |
| leaning_forward.jpg | https://unsplash.com/photos/zzTsoP5OeBs | @theian20 |

Missing postures (kneeling, crouching, lying) fall back to the drawn `synthetic:` figure.
