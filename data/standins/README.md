# Photo stand-ins

Rehearsal / no-camera stand-ins for the participant (`--source photo:<posture>`, and the
"Start with a sample photo" button). The real pose estimator reads them, so the whole pipeline
(pose → painting match → 3-D avatar) runs as with a person in front of the camera.

All from [Unsplash](https://unsplash.com), [Unsplash License](https://unsplash.com/license) (free to use, no permission needed).

| File | Photo | Photographer |
| --- | --- | --- |
| sitting.jpg | https://unsplash.com/photos/EJ08zy1a-UI | @davidprspctive |
| standing.jpg | https://unsplash.com/photos/ymo_yC_N_2o | @mitchel3uo |
| standing_4.jpg | https://unsplash.com/photos/_pG1u1_iItM | @bennieray |
| standing_2.jpg | https://unsplash.com/@minimalism_brand (search "person standing full body") | @minimalism_brand |
| standing_3.jpg | https://unsplash.com/photos/skUWW81OBeY | @tostilse |
| squatting.jpg | https://unsplash.com/photos/5_G4i0NRLx4 | @jawnwalrus |
| sitting_2.jpg | https://unsplash.com/photos/d1gfVswXpLk | @olenkasergienko |

The sample start ("Start with a sample photo") steps through all of them, one per round (SAMPLE_ORDER in
performance/runtime.py). Missing postures (kneeling, crouching, lying, leaning forward) fall back to the drawn `synthetic:` figure.
