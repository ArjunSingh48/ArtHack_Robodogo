// The visitor's pose, read in the browser: MediaPipe Pose Landmarker (same model as the Python show) on the
// webcam, and the same geometric posture rules as src/is_this_us/vision/interpreter.py (ported 1:1).
import { FilesetResolver, PoseLandmarker } from 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs';

const MODEL = 'https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_lite/float16/latest/pose_landmarker_lite.task';
const VIS = 0.5;
const L_SH = 11, R_SH = 12, L_HIP = 23, R_HIP = 24, L_KNEE = 25, R_KNEE = 26, L_ANK = 27, R_ANK = 28;

let landmarker = null;
export async function loadPose() {
  if (landmarker) return landmarker;
  const files = await FilesetResolver.forVisionTasks('https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm');
  landmarker = await PoseLandmarker.createFromOptions(files, {
    baseOptions: { modelAssetPath: MODEL, delegate: 'GPU' }, runningMode: 'VIDEO', numPoses: 1,
  });
  return landmarker;
}

// ---------------------------------------------------------------- posture (interpreter.py)
const ramp = (x, lo, hi) => (lo === hi ? +(x >= hi) : Math.min(1, Math.max(0, (x - lo) / (hi - lo))));
const sub = (a, b) => [a[0] - b[0], a[1] - b[1]];
const norm = (v) => Math.hypot(v[0], v[1]);
const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2];
function angle(a, b, c) {
  const v1 = sub(a, b), v2 = sub(c, b), n = norm(v1) * norm(v2);
  if (n < 1e-6) return 180;
  return (Math.acos(Math.min(1, Math.max(-1, (v1[0] * v2[0] + v1[1] * v2[1]) / n))) * 180) / Math.PI;
}
const fromVertical = (v) => { const n = norm(v); return n < 1e-6 ? 0 : (Math.acos(Math.min(1, Math.abs(v[1]) / n)) * 180) / Math.PI; };
const avg = (a) => a.reduce((s, x) => s + x, 0) / a.length;

function features(xy, vis) {
  if (Math.min(vis[L_SH], vis[R_SH]) < VIS || Math.max(vis[L_HIP], vis[R_HIP]) < VIS) return null;
  const sh = mid(xy[L_SH], xy[R_SH]), hip = mid(xy[L_HIP], xy[R_HIP]), torso = sub(sh, hip), T = norm(torso);
  if (T < 5) return null;
  const f = { T, tilt: fromVertical(torso), legs: false };
  const legs = [[L_HIP, L_KNEE, L_ANK], [R_HIP, R_KNEE, R_ANK]].filter(([h, k]) => Math.min(vis[h], vis[k]) >= VIS);
  if (!legs.length) return f;
  f.legs = true;
  const thigh = [], knee = [], hipY = [], kneeY = [];
  for (const [h, k, a] of legs) {
    thigh.push(fromVertical(sub(xy[k], xy[h])));
    if (vis[a] >= VIS) {
      knee.push(angle(xy[h], xy[k], xy[a]));
      hipY.push((xy[a][1] - xy[h][1]) / T);
      kneeY.push((xy[a][1] - xy[k][1]) / T);
    }
  }
  f.thigh = avg(thigh);
  if (knee.length) { f.knee = avg(knee); f.hipAbove = avg(hipY); f.kneeAbove = avg(kneeY); }
  return f;
}

function scores(f) {
  const s = {}, t = f.tilt, upright = ramp(t, 40, 15), horizontal = ramp(t, 50, 75);
  s.lying = horizontal;
  s.reclining = ramp(t, 25, 40) * ramp(t, 75, 55);
  if (!f.legs || f.knee === undefined) {
    s.unknown = 0.6 * upright;
    if (f.legs) { s.sitting = upright * ramp(f.thigh, 40, 65) * 0.8; s.standing = upright * ramp(f.thigh, 30, 12) * 0.7; }
    return s;
  }
  s.reclining *= ramp(f.thigh, 30, 55);
  const straight = ramp(f.knee, 140, 165), bent = ramp(f.knee, 140, 100);
  const thighV = ramp(f.thigh, 35, 15), thighH = ramp(f.thigh, 40, 65);
  const tall = ramp(f.hipAbove, 1.0, 1.5), low = ramp(f.hipAbove, 0.9, 0.5), kneeFloor = ramp(f.kneeAbove, 0.45, 0.15);
  s.standing = upright * straight * thighV * tall;
  s.leaning_forward = ramp(t, 18, 35) * ramp(t, 70, 55) * thighV * ramp(f.knee, 120, 150);
  s.sitting = ramp(t, 50, 30) * thighH * ramp(f.knee, 50, 75) * (1 - low * 0.5);
  s.crouching = bent * low * ramp(f.knee, 15, 35) * (1 - kneeFloor * 0.7);
  s.kneeling = kneeFloor * ramp(f.thigh, 70, 40) * ramp(t, 55, 30) * ramp(f.knee, 150, 120);
  s.lying = Math.max(s.lying, horizontal * ramp(f.thigh, 50, 75));
  return s;
}

// → { visible, posture, framing, points [[x,y,vis] 0..1], skeleton (avatar frame, mirrored like the view) }
export function read(video, swapLR, now = performance.now()) {
  if (!landmarker || !video.videoWidth) return { visible: false };
  const res = landmarker.detectForVideo(video, now);
  const lm = res.landmarks && res.landmarks[0];
  if (!lm) return { visible: false };
  const w = video.videoWidth, h = video.videoHeight;
  const xy = lm.map((p) => [p.x * w, p.y * h]), vis = lm.map((p) => p.visibility ?? 1);
  const f = features(xy, vis);
  let posture = null;
  if (f) {
    const ranked = Object.entries(scores(f)).sort((a, b) => b[1] - a[1]);
    if (ranked.length && ranked[0][1] >= 0.25 && ranked[0][0] !== 'unknown') posture = ranked[0][0];
  }
  const framing = !f ? 'partial' : f.legs ? 'ok' : 'partial';
  // the view is mirrored (like a mirror): x flipped, and left/right landmarks swapped
  const points = lm.map((p, i) => [1 - p.x, p.y, vis[i]]);
  const mirroredPoints = swapLR.map((j) => points[j]);
  const wl = res.worldLandmarks && res.worldLandmarks[0];
  const skeleton = wl ? swapLR.map((j) => [-wl[j].x, -wl[j].y, -wl[j].z, vis[j]]) : null;
  return { visible: true, posture, framing, points: mirroredPoints, skeleton };
}
