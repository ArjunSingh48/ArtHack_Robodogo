// The simulated room in 3-D (three.js): the Unitree Go2 (official URDF + meshes, animated per joint) and a
// rigged human avatar posed from the photo camera's pose estimate. Fed by the same /state snapshot as the
// drawn map (stage.py drawSim), which stays as the fallback when this cannot load (offline, no WebGL).
//
// Sim coordinates: x forward from the dog's start, y to its left, metres. three.js: y up, so sim (x, y) →
// three (-y, 0, -x). The camera stands behind the dog's start, like the audience, and frames dog + person by
// itself until someone drags (rotate), right-drags (pan) or scrolls (zoom); a double-click hands it back.
import * as THREE from 'three';
import { GLTFLoader } from 'three/examples/jsm/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/examples/jsm/controls/OrbitControls.js';
import URDFLoader from 'urdf-loader';

const GO2 = 'https://cdn.jsdelivr.net/gh/unitreerobotics/unitree_ros@15ebe53f4cd77e7d745ae83b9d7ff90e685e8998/robots/go2_description';
const AVATAR = 'https://cdn.jsdelivr.net/gh/mrdoob/three.js@r170/examples/models/gltf/Michelle.glb';
const AVATAR_HEIGHT = 1.65;
const W = (x, y, h = 0) => new THREE.Vector3(-y, h, -x);

// ---------------------------------------------------------------- Go2 poses (URDF joint angles, rad)
// thigh + = foot backward, calf always negative (knee bends backward); pitch + = nose down
const LEG = (thigh, calf, hip = 0) => ({ hip, thigh, calf });
const POSES = {
  stand: { pitch: 0, legs: { F: LEG(0.67, -1.3), R: LEG(0.67, -1.3) } },
  sit: { pitch: -0.55, legs: { F: LEG(0.75, -1.0), R: LEG(1.6, -2.6) } },
  lie: { pitch: 0, legs: { F: LEG(1.15, -2.7), R: LEG(1.15, -2.7) } },
  stretch: { pitch: 0.3, legs: { F: LEG(0.2, -2.2), R: LEG(0.9, -1.4) } },
};
const LEGS = ['FL', 'FR', 'RL', 'RR'];

function dogPose(m) {
  const a = m.action || '';
  if (a === 'Sit' || a === 'Hello' || (m.body === 'sitting' && a !== 'RiseSit')) return a === 'Hello' ? 'hello' : 'sit';
  if (a === 'StandDown' || (m.body === 'lying' && a !== 'StandUp')) return 'lie';
  if (a === 'Stretch') return 'stretch';
  return 'stand';
}

// ---------------------------------------------------------------- avatar presets (when no camera reading)
// MediaPipe indices; avatar frame: x = the avatar's left (viewer's right), y up, z toward the viewer, hips at 0
const L = { sh: 11, el: 13, wr: 15, hip: 23, kn: 25, an: 27 }, R = { sh: 12, el: 14, wr: 16, hip: 24, kn: 26, an: 28 };
function preset(p) {
  const s = (o) => { const out = []; for (const [k, v] of Object.entries(o)) { out[L[k]] = v; out[R[k]] = [-v[0], v[1], v[2]]; } return out; };
  const base = {
    sh: [0.18, 0.5, 0], el: [0.22, 0.22, 0.02], wr: [0.24, -0.02, 0.06],
    hip: [0.1, 0, 0], kn: [0.11, -0.45, 0.02], an: [0.11, -0.88, 0],
  };
  const P = {
    standing: base,
    sitting: { ...base, el: [0.22, 0.24, 0.1], wr: [0.16, 0.06, 0.32], kn: [0.12, -0.02, 0.45], an: [0.12, -0.45, 0.5] },
    kneeling: { ...base, kn: [0.11, -0.45, 0.05], an: [0.11, -0.5, -0.4] },
    crouching: { ...base, sh: [0.18, 0.4, 0.25], el: [0.2, 0.18, 0.4], wr: [0.16, -0.05, 0.45],
                 kn: [0.15, -0.1, 0.42], an: [0.13, -0.5, 0.1] },
    leaning_forward: { ...base, sh: [0.18, 0.4, 0.3], el: [0.2, 0.15, 0.4], wr: [0.2, -0.08, 0.45] },
  };
  if (p === 'lying') {  // on the side, facing the viewer, head to the viewer's left
    const o = [];
    const put = (i, v) => { o[i] = v; };
    put(11, [-0.5, 0.18, 0]); put(12, [-0.5, -0.18, 0]);
    put(13, [-0.3, 0.22, 0.15]); put(15, [-0.1, 0.2, 0.3]);
    put(14, [-0.55, -0.2, 0.25]); put(16, [-0.75, -0.18, 0.2]);
    put(23, [0, 0.1, 0]); put(24, [0, -0.1, 0]);
    put(25, [0.45, 0.1, 0.05]); put(26, [0.45, -0.1, 0.05]);
    put(27, [0.88, 0.1, 0]); put(28, [0.88, -0.1, 0]);
    return o;
  }
  return s(P[p] || base);
}

// ---------------------------------------------------------------- avatar retargeting
const V = (a) => new THREE.Vector3(a[0], a[1], a[2]);
const mid = (a, b) => V(a).add(V(b)).multiplyScalar(0.5);
// bone, child bone (for its rest direction), landmark from → to
const LIMBS = [
  ['LeftArm', 'LeftForeArm', 11, 13], ['LeftForeArm', 'LeftHand', 13, 15],
  ['RightArm', 'RightForeArm', 12, 14], ['RightForeArm', 'RightHand', 14, 16],
  ['LeftUpLeg', 'LeftLeg', 23, 25], ['LeftLeg', 'LeftFoot', 25, 27],
  ['RightUpLeg', 'RightLeg', 24, 26], ['RightLeg', 'RightFoot', 26, 28],
];

function basis(up, left) {  // orthonormal frame from an up and a (roughly) left vector
  const u = up.clone().normalize(), l = left.clone().addScaledVector(u, -left.dot(u)).normalize();
  const f = new THREE.Vector3().crossVectors(l, u);
  return new THREE.Matrix4().makeBasis(l, u, f);
}

function makeAvatar(gltf) {
  const root = gltf.scene, bones = {};
  root.traverse((o) => {
    if (o.isBone) bones[o.name.replace(/^mixamorig:?/, '')] = o;
    if (o.isMesh) { o.castShadow = true; o.frustumCulled = false; }
  });
  const rest = {};
  for (const [k, b] of Object.entries(bones)) rest[k] = b.quaternion.clone();
  const box = new THREE.Box3().setFromObject(root);
  root.scale.setScalar(AVATAR_HEIGHT / (box.max.y - box.min.y));
  const holder = new THREE.Group(); holder.add(root);  // holder: placement + facing; root: grounding
  const wp = (n) => bones[n].getWorldPosition(new THREE.Vector3());
  const wq = (o) => o.getWorldQuaternion(new THREE.Quaternion());
  const smooth = {};

  function setWorldRotation(bone, q) {  // bone's world rotation := q
    const parent = wq(bone.parent).invert();
    bone.quaternion.copy(parent.multiply(q));
    bone.updateMatrixWorld(true);
  }

  function pose(lm) {
    for (const [k, b] of Object.entries(bones)) b.quaternion.copy(rest[k]);
    holder.updateMatrixWorld(true);
    const face = wq(holder);  // the avatar frame → world
    const toWorld = (v) => v.applyQuaternion(face);
    // torso: hips→shoulders up, right hip→left hip
    const up = toWorld(mid(lm[11], lm[12]).sub(mid(lm[23], lm[24])));
    const left = toWorld(V(lm[23]).sub(V(lm[24])));
    const restUp = wp('Neck').sub(wp('Hips')), restLeft = wp('LeftUpLeg').sub(wp('RightUpLeg'));
    const delta = new THREE.Quaternion().setFromRotationMatrix(
      basis(up, left).multiply(basis(restUp, restLeft).invert()));
    setWorldRotation(bones.Hips, delta.multiply(wq(bones.Hips)));
    for (const [bn, child, a, b] of LIMBS) {
      if (!lm[a] || !lm[b] || (lm[a][3] ?? 1) < 0.3 || (lm[b][3] ?? 1) < 0.3) continue;
      const cur = wp(child).sub(wp(bn)).normalize();
      const want = toWorld(V(lm[b]).sub(V(lm[a]))).normalize();
      setWorldRotation(bones[bn], new THREE.Quaternion().setFromUnitVectors(cur, want).multiply(wq(bones[bn])));
    }
    // ease from the last frame's pose (camera readings jitter)
    for (const [k, b] of Object.entries(bones)) {
      if (smooth[k]) b.quaternion.copy(smooth[k].slerp(b.quaternion, 0.35));
      smooth[k] = b.quaternion.clone();
    }
    // stand on the floor: the lowest joint touches it
    root.position.y = 0;
    holder.updateMatrixWorld(true);
    let low = Infinity;
    for (const n of ['LeftToeBase', 'RightToeBase', 'LeftFoot', 'RightFoot', 'LeftLeg', 'RightLeg', 'Hips',
                     'LeftHand', 'RightHand', 'Head', 'LeftArm', 'RightArm', 'Spine2'])
      if (bones[n]) low = Math.min(low, wp(n).y);
    root.position.y = (holder.position.y - low) / holder.scale.y + 0.04;
    holder.updateMatrixWorld(true);
    return { hips: wp('Hips'), knee: wp('LeftLeg'), ankle: wp('LeftFoot') };
  }
  return { holder, pose };
}

// ---------------------------------------------------------------- scene
export function start(el) {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  el.appendChild(renderer.domElement);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x26232a);
  const camera = new THREE.PerspectiveCamera(42, 1, 0.05, 50);
  const look = W(1.3, 0.2, 0.45), BEHIND = W(-1, 0, 0.62).normalize();  // seen from the audience, a little above
  camera.position.copy(look).addScaledVector(BEHIND, 3);
  camera.lookAt(look);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.copy(look);
  controls.maxPolarAngle = Math.PI / 2 - 0.05;  // stay above the floor
  controls.enableDamping = true;
  let manual = false;
  controls.addEventListener('start', () => { manual = true; });
  renderer.domElement.addEventListener('dblclick', () => { manual = false; });
  function follow(a, b) {  // keep the dog and the person framed
    if (manual) return;
    const want = a.clone().add(b).multiplyScalar(0.5).setY(0.45);
    look.lerp(want, 0.05);
    const dist = Math.max(2.2, a.distanceTo(b) * 1.3 + 1.6);
    camera.position.lerp(look.clone().addScaledVector(BEHIND, dist), 0.05);
    controls.target.copy(look);
  }

  scene.add(new THREE.HemisphereLight(0xefe9de, 0x2a2730, 1.6));
  const sun = new THREE.DirectionalLight(0xffffff, 2.2);
  sun.position.set(-2, 5, 3); sun.castShadow = true;
  sun.shadow.mapSize.set(1024, 1024);
  Object.assign(sun.shadow.camera, { left: -4, right: 4, top: 4, bottom: -4 });
  scene.add(sun);
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(14, 14), new THREE.MeshStandardMaterial({ color: 0x302c35 }));
  floor.rotation.x = -Math.PI / 2; floor.receiveShadow = true; scene.add(floor);
  const grid = new THREE.GridHelper(14, 14, 0x4a4550, 0x3a363d); grid.position.y = 0.002; scene.add(grid);
  const trailGeo = new THREE.BufferGeometry();
  const trail = new THREE.Line(trailGeo, new THREE.LineDashedMaterial({ color: 0x8fc7d9, dashSize: 0.06, gapSize: 0.05 }));
  trail.position.y = 0.01; scene.add(trail);
  const chair = new THREE.Mesh(new THREE.BoxGeometry(0.45, 1, 0.45), new THREE.MeshStandardMaterial({ color: 0x5b4f43 }));
  chair.castShadow = chair.receiveShadow = true; chair.visible = false; scene.add(chair);

  // --- the dog
  const dog = new THREE.Group(), body = new THREE.Group();  // dog: position + heading; body: height + pitch
  dog.add(body); scene.add(dog);
  let robot = null;
  const joints = {}, butt = new THREE.Object3D();
  const loader = new URDFLoader();
  loader.packages = { go2_description: GO2 };
  loader.load(`${GO2}/urdf/go2_description.urdf`, (r) => {
    r.rotation.x = -Math.PI / 2;  // URDF is z-up
    r.traverse((o) => { if (o.isMesh) { o.castShadow = true; } });
    for (const leg of LEGS) for (const j of ['hip', 'thigh', 'calf']) joints[`${leg}_${j}`] = POSES.stand.legs[leg[0]][j];
    butt.position.set(-0.2, 0, -0.06); r.links.base.add(butt);  // the rear of the body sits on the floor
    body.add(r); robot = r;
  }, undefined, (e) => console.warn('Go2 model failed to load', e));

  // --- the person
  let avatar = null;
  new GLTFLoader().load(AVATAR, (g) => { avatar = makeAvatar(g); scene.add(avatar.holder); },
    undefined, (e) => console.warn('avatar failed to load', e));

  // --- minimap: the room from above, top-right (dog heading + where it started); north = the dog's starting direction
  const mapCam = new THREE.OrthographicCamera(-2, 2, 2, -2, 0.1, 30);
  mapCam.up.set(0, 0, -1);
  mapCam.layers.enable(1);
  const marker = (geo, color) => {
    const m = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color }));
    m.rotation.x = -Math.PI / 2; m.position.y = 1.9; m.layers.set(1); scene.add(m); return m;
  };
  const dogMark = marker(new THREE.ConeGeometry(0.13, 0.32, 3), 0x8fc7d9);  // points the way the dog faces
  dogMark.rotation.order = 'YXZ';
  const startMark = marker(new THREE.RingGeometry(0.1, 0.14, 24), 0x9c958c);

  let state = null, pitch = 0, phase = 0, last = performance.now();
  let prev = null, speed = 0;  // forward speed in the dog's own frame (m/s, − = walking backwards)
  const target = {};

  function frame(now) {
    const dt = Math.min(0.1, (now - last) / 1000); last = now;
    if (state && robot) {
      const m = state, name = dogPose(m), P = POSES[name === 'hello' ? 'sit' : name];
      for (const leg of LEGS) for (const j of ['hip', 'thigh', 'calf']) target[`${leg}_${j}`] = P.legs[leg[0]][j];
      if (name === 'hello') {  // the right front paw up, waving
        target.FR_thigh = -0.5 + 0.25 * Math.sin(now / 160); target.FR_calf = -1.0;
      }
      let bob = 0;
      if (m.walking && name === 'stand') {  // trot: diagonal pairs swing together
        // step the way it actually moves: retracing its path home it walks backwards
        const dir = speed < -0.02 ? -1 : 1;
        phase += dt * 2 * Math.PI * 2.2 * dir;
        for (const leg of LEGS) {
          const p = phase + (leg === 'FL' || leg === 'RR' ? 0 : Math.PI);
          target[`${leg}_thigh`] = 0.67 - 0.25 * Math.sin(p);
          target[`${leg}_calf`] = -1.3 - 0.45 * Math.max(0, Math.cos(p));
        }
        bob = 0.008 * Math.cos(2 * phase);
      }
      const k = m.walking ? 1 : Math.min(1, dt * 3);  // poses ease in; the gait is followed directly
      for (const [j, v] of Object.entries(target)) {
        joints[j] += (v - joints[j]) * k;
        robot.setJointValue(`${j}_joint`, joints[j]);
      }
      pitch += (P.pitch - pitch) * Math.min(1, dt * 3);
      const [rx, ry, ryaw] = m.robot;
      dog.position.copy(W(rx, ry));
      dog.rotation.y = ryaw + Math.PI / 2;
      body.rotation.z = -pitch;
      body.position.y = 0;
      dog.updateMatrixWorld(true);
      let low = Infinity;
      robot.traverse((o) => { if (/_foot$/.test(o.name)) low = Math.min(low, o.getWorldPosition(new THREE.Vector3()).y); });
      low = Math.min(low, butt.getWorldPosition(new THREE.Vector3()).y);
      body.position.y = -low + 0.022 + bob;  // foot sphere radius

      const pts = (m.trail || []).map(([x, y]) => W(x, y));
      if (pts.length > 1) { trailGeo.setFromPoints(pts); trail.computeLineDistances(); }
    }
    if (state && avatar) {
      const [px, py] = state.person, h = avatar.holder;
      h.position.copy(W(px, py));
      h.rotation.y = Math.atan2(BEHIND.x, BEHIND.z);  // faces the audience's camera, also while the view is orbited
      // what the camera sees; joints it cannot see (legs out of frame…) from the posture preset — both hip-centred
      const pre = preset(state.posture);
      const lm = state.skeleton ? state.skeleton.map((p, i) => (p[3] >= 0.5 || !pre[i] ? p : pre[i])) : pre;
      const j = avatar.pose(lm);
      const sitting = j.hips.y - j.knee.y < 0.2 && j.knee.y - j.ankle.y > 0.25;  // thighs level, shins upright
      chair.visible = sitting;
      if (sitting) {
        const seat = Math.max(0.1, j.hips.y - 0.1);
        chair.scale.y = seat; chair.position.set(j.hips.x, seat / 2, j.hips.z);
        chair.rotation.y = h.rotation.y;
        chair.position.addScaledVector(new THREE.Vector3(Math.sin(h.rotation.y), 0, Math.cos(h.rotation.y)), -0.08);
      }
    }
    if (state) follow(W(state.robot[0], state.robot[1]), W(state.person[0], state.person[1]));
    if (state) {
      const d = W(state.robot[0], state.robot[1]), p = W(state.person[0], state.person[1]), o = W(0, 0);
      dogMark.position.set(d.x, 1.9, d.z); dogMark.rotation.set(-Math.PI / 2, state.robot[2], 0);
      startMark.position.set(o.x, 1.9, o.z);
      const box = new THREE.Box3().setFromPoints([d, p, o]), c = box.getCenter(new THREE.Vector3());
      const half = Math.max(1.4, (Math.max(box.max.x - box.min.x, box.max.z - box.min.z)) / 2 + 0.6);
      Object.assign(mapCam, { left: -half, right: half, top: half, bottom: -half });
      mapCam.position.set(c.x, 10, c.z); mapCam.lookAt(c.x, 0, c.z); mapCam.updateProjectionMatrix();
    }
    const w = el.clientWidth, hgt = el.clientHeight;
    if (w && hgt && (renderer.domElement.width !== Math.floor(w * renderer.getPixelRatio()) ||
                     renderer.domElement.height !== Math.floor(hgt * renderer.getPixelRatio()))) {
      renderer.setSize(w, hgt, false);
      renderer.domElement.style.width = '100%'; renderer.domElement.style.height = '100%';
      camera.aspect = w / hgt; camera.updateProjectionMatrix();
    }
    controls.update();
    renderer.setViewport(0, 0, w, hgt);
    renderer.render(scene, camera);
    if (state && w && hgt) {  // the minimap, drawn over the top-right corner
      const S = Math.floor(Math.min(w, hgt) * 0.36), x = w - S - 8, y = hgt - S - 8;
      renderer.setScissorTest(true);
      renderer.setScissor(x - 2, y - 2, S + 4, S + 4); renderer.setClearColor(0x8fc7d9); renderer.clear();
      renderer.setScissor(x, y, S, S); renderer.setViewport(x, y, S, S);
      renderer.setClearColor(0x1b191e); renderer.clear();
      renderer.render(scene, mapCam);
      renderer.setScissorTest(false);
    }
    requestAnimationFrame(frame);
  }
  requestAnimationFrame(frame);
  function update(m) {
    const t = performance.now() / 1000;
    if (prev && t - prev.t > 0.05) {
      const [x0, y0] = prev.robot, [x1, y1, yaw] = m.robot;
      const fwd = ((x1 - x0) * Math.cos(yaw) + (y1 - y0) * Math.sin(yaw)) / (t - prev.t);
      speed += (fwd - speed) * 0.5;
      prev = { t, robot: m.robot };
    } else if (!prev) prev = { t, robot: m.robot };
    state = m;
  }
  return { ready: () => robot !== null, update };
}
