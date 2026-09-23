"""
Three.js 3D pallet viewer.

``build_viewer_html`` returns a fully self-contained HTML document (Three.js +
OrbitControls loaded from a CDN) that renders one pallet's boxes as colored,
orbitable cubes. It is embedded in Streamlit via ``st.components.v1.html``.

Coordinate mapping (pallet space -> Three.js world):
    world X = pallet length, world Y = height (up), world Z = pallet width.
"""

from __future__ import annotations

from typing import Dict, List
import json

from engine import Placement, color_for_name


def build_viewer_html(
    placements: List[Placement],
    pallet_length: float,
    pallet_width: float,
    overhang: float,
    unit_label: str = "in",
    height_px: int = 640,
    max_height: float = 0.0,
) -> str:
    boxes = [
        {
            "name": p.box_name,
            "x": p.x,
            "y": p.y,
            "z": p.z,
            "l": p.length,
            "w": p.width,
            "h": p.height,
            "layer": p.layer,
            "weight": p.weight,
            "color": p.color or color_for_name(p.box_name),
        }
        for p in placements
    ]

    # Legend entries (one per box type, with count).
    counts: Dict[str, int] = {}
    for p in placements:
        counts[p.box_name] = counts.get(p.box_name, 0) + 1
    legend = [
        {"name": name, "color": color_for_name(name), "count": counts[name]}
        for name in sorted(counts)
    ]

    max_layer = max((p.layer for p in placements), default=1)

    payload = json.dumps(
        {
            "boxes": boxes,
            "legend": legend,
            "palletLength": pallet_length,
            "palletWidth": pallet_width,
            "overhang": overhang,
            "unit": unit_label,
            "maxLayer": max_layer,
            "maxHeight": max_height,
        }
    )

    return _TEMPLATE.replace("/*__DATA__*/", payload).replace("__HEIGHT__", str(height_px))


_TEMPLATE = r"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  html, body { margin: 0; padding: 0; overflow: hidden; }
  #wrap { position: relative; width: 100%; height: __HEIGHT__px;
          font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #0e1117; }
  #canvas { width: 100%; height: 100%; display: block; }
  .panel { position: absolute; background: rgba(20,24,33,0.86); color: #e6e6e6;
           border: 1px solid #2b3340; border-radius: 8px; padding: 10px 12px; font-size: 13px; }
  #controls { top: 10px; left: 10px; width: 210px; }
  #controls label { display: block; margin: 8px 0 3px; color: #b9c2d0; }
  #controls .row { display: flex; align-items: center; gap: 8px; }
  #controls input[type=range] { width: 100%; }
  #legend { top: 10px; right: 10px; max-height: 60%; overflow-y: auto; }
  #legend .item { display: flex; align-items: center; gap: 8px; margin: 4px 0; }
  #legend .sw { width: 14px; height: 14px; border-radius: 3px; border: 1px solid #000; }
  #tooltip { position: absolute; pointer-events: none; background: rgba(0,0,0,0.85);
             color: #fff; padding: 6px 8px; border-radius: 6px; font-size: 12px;
             display: none; white-space: nowrap; z-index: 10; }
  button { background: #2b6cb0; color: #fff; border: none; border-radius: 6px;
           padding: 6px 10px; cursor: pointer; font-size: 12px; width: 100%; margin-top: 8px; }
  button:hover { background: #3182ce; }
  .hint { font-size: 11px; color: #7c87a0; margin-top: 6px; }
</style>
</head>
<body>
<div id="wrap">
  <canvas id="canvas"></canvas>
  <div id="controls" class="panel">
    <div style="font-weight:600;margin-bottom:4px;">Pallet view</div>
    <label>Show layers up to: <span id="layerVal"></span></label>
    <input id="layerSlider" type="range" min="1" step="1">
    <div class="row" style="margin-top:8px;">
      <input type="checkbox" id="solidChk" checked><span>Solid boxes</span>
    </div>
    <div class="row">
      <input type="checkbox" id="labelChk"><span>Box labels</span>
    </div>
    <button id="resetBtn">Reset camera</button>
    <div class="hint">Drag = orbit · Right-drag = pan · Scroll = zoom · Hover a box for details · Hover a corner ruler for height · Green loop = current top</div>
  </div>
  <div id="legend" class="panel"></div>
  <div id="tooltip"></div>
</div>

<script src="https://unpkg.com/three@0.128.0/build/three.min.js"></script>
<script src="https://unpkg.com/three@0.128.0/examples/js/controls/OrbitControls.js"></script>
<script>
const DATA = /*__DATA__*/;

const wrap = document.getElementById('wrap');
const canvas = document.getElementById('canvas');
const tooltip = document.getElementById('tooltip');

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0e1117);

const camera = new THREE.PerspectiveCamera(55, wrap.clientWidth / wrap.clientHeight, 0.1, 100000);
const renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(wrap.clientWidth, wrap.clientHeight);

const controls = new THREE.OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;

// Lights
scene.add(new THREE.AmbientLight(0xffffff, 0.65));
const dir = new THREE.DirectionalLight(0xffffff, 0.7);
dir.position.set(1, 2, 1.4);
scene.add(dir);
const dir2 = new THREE.DirectionalLight(0xffffff, 0.25);
dir2.position.set(-1, 1, -1);
scene.add(dir2);

const PL = DATA.palletLength, PW = DATA.palletWidth, OV = DATA.overhang;

// Map pallet coords -> world (center the pallet at origin).
function cx(x) { return x - PL / 2; }
function cz(y) { return y - PW / 2; }

// Pallet deck slab.
const deckH = Math.max(2, Math.min(PL, PW) * 0.05);
const deckGeo = new THREE.BoxGeometry(PL, deckH, PW);
const deckMat = new THREE.MeshLambertMaterial({ color: 0x8a6d3b });
const deck = new THREE.Mesh(deckGeo, deckMat);
deck.position.set(0, -deckH / 2, 0);
scene.add(deck);
const deckEdges = new THREE.LineSegments(
  new THREE.EdgesGeometry(deckGeo),
  new THREE.LineBasicMaterial({ color: 0x000000 })
);
deckEdges.position.copy(deck.position);
scene.add(deckEdges);

// Allowed-footprint outline (incl. overhang) on the deck top.
const fpW = PL + 2 * OV, fpD = PW + 2 * OV;
const fpGeo = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(-fpW/2, 0.1, -fpD/2), new THREE.Vector3( fpW/2, 0.1, -fpD/2),
  new THREE.Vector3( fpW/2, 0.1,  fpD/2), new THREE.Vector3(-fpW/2, 0.1,  fpD/2),
  new THREE.Vector3(-fpW/2, 0.1, -fpD/2),
]);
const fpLine = new THREE.Line(fpGeo, new THREE.LineDashedMaterial({ color: 0x8899aa, dashSize: 3, gapSize: 2 }));
fpLine.computeLineDistances();
scene.add(fpLine);

// Ground grid for orientation.
const grid = new THREE.GridHelper(Math.max(PL, PW) * 3, 24, 0x334155, 0x1e2733);
grid.position.y = -deckH;
scene.add(grid);

// Build boxes.
const boxMeshes = [];
const labelSprites = [];

function makeLabel(text, scaleMul, color, alwaysOnTop) {
  scaleMul = scaleMul || 0.06;
  color = color || '#ffffff';
  const onTop = !!alwaysOnTop;  // default: respect depth so boxes occlude the label
  const cv = document.createElement('canvas');
  const ctx = cv.getContext('2d');
  ctx.font = '28px sans-serif';
  const w = ctx.measureText(text).width + 16;
  cv.width = w; cv.height = 40;
  ctx.font = '28px sans-serif';
  ctx.fillStyle = 'rgba(0,0,0,0.7)';
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = color;
  ctx.textBaseline = 'middle';
  ctx.fillText(text, 8, 22);
  const tex = new THREE.CanvasTexture(cv);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, depthTest: !onTop, depthWrite: false }));
  sprite.renderOrder = onTop ? 999 : 0;
  const scale = Math.max(PL, PW) * scaleMul;
  sprite.scale.set(scale * (cv.width / cv.height), scale, 1);
  return sprite;
}

DATA.boxes.forEach((b) => {
  const geo = new THREE.BoxGeometry(b.l, b.h, b.w);
  const mat = new THREE.MeshLambertMaterial({ color: new THREE.Color(b.color) });
  const mesh = new THREE.Mesh(geo, mat);
  mesh.position.set(cx(b.x) + b.l / 2, b.z + b.h / 2, cz(b.y) + b.w / 2);
  mesh.userData = b;
  scene.add(mesh);
  boxMeshes.push(mesh);

  const edges = new THREE.LineSegments(
    new THREE.EdgesGeometry(geo),
    new THREE.LineBasicMaterial({ color: 0x111111 })
  );
  edges.position.copy(mesh.position);
  mesh.userData.edges = edges;
  scene.add(edges);

  const label = makeLabel(b.name);
  label.position.set(mesh.position.x, b.z + b.h + Math.max(PL, PW) * 0.03, mesh.position.z);
  label.visible = false;
  mesh.userData.label = label;
  labelSprites.push(label);
  scene.add(label);
});

// Camera framing.
const loadedH = DATA.boxes.reduce((m, b) => Math.max(m, b.z + b.h), deckH);
const span = Math.max(PL, PW, loadedH);

// ---- Persistent reference rulers (length / width / height) ----
// Live in their own group so the layer slider never hides them.
const rulers = new THREE.Group();
scene.add(rulers);
const U = DATA.unit || '';

function niceStep(range) {
  const target = range / 6;
  const pow = Math.pow(10, Math.floor(Math.log10(Math.max(target, 1e-6))));
  const cands = [1, 2, 5, 10].map((m) => m * pow);
  return cands.reduce((b, c) => (Math.abs(c - target) < Math.abs(b - target) ? c : b), cands[0]);
}

const OFF = Math.max(PL, PW) * 0.04;
const TICK = Math.max(PL, PW) * 0.025;
const rulerLineMat = new THREE.LineBasicMaterial({ color: 0x9fb3c8 });
const rulerHits = [];  // invisible hover targets for the height rulers

// Ground ruler: origin + dir along axis, tick pointing outward.
function addGroundRuler(origin, dir, tick, length, step, labelText) {
  const end = origin.clone().add(dir.clone().multiplyScalar(length));
  rulers.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([origin, end]), rulerLineMat));
  for (let v = 0; v <= length + 1e-6; v += step) {
    const p = origin.clone().add(dir.clone().multiplyScalar(v));
    rulers.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(
      [p, p.clone().add(tick.clone().multiplyScalar(TICK))]), rulerLineMat));
    const lab = makeLabel(String(Math.round(v)), 0.034, '#cfe0f0');  // depth-tested
    lab.position.copy(p.clone().add(tick.clone().multiplyScalar(TICK * 2.4)));
    rulers.add(lab);
  }
  const cap = makeLabel(labelText, 0.042, '#9fb3c8');
  cap.position.copy(end.clone().add(tick.clone().multiplyScalar(TICK * 2.4)));
  rulers.add(cap);
}

const heightRange = Math.max(loadedH, DATA.maxHeight || 0);
const vStep = niceStep(heightRange);
const vTop = Math.ceil(heightRange / vStep) * vStep;

// Height ruler on ALL FOUR corners (so a scale is always visible as you orbit),
// but only one corner carries the numbers, and they are depth-tested so boxes
// occlude them instead of bleeding through. Hover anywhere on a ruler for the
// exact height.
function addHeightRuler(sx, sz, withLabels) {
  const bx = sx * (PL / 2 + OFF), bz = sz * (PW / 2 + OFF);
  const tick = new THREE.Vector3(sx, 0, sz).normalize();
  rulers.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(
    [new THREE.Vector3(bx, 0, bz), new THREE.Vector3(bx, vTop, bz)]), rulerLineMat));
  for (let v = 0; v <= vTop + 1e-6; v += vStep) {
    const p = new THREE.Vector3(bx, v, bz);
    rulers.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(
      [p, p.clone().add(tick.clone().multiplyScalar(TICK))]), rulerLineMat));
    if (withLabels) {
      const lab = makeLabel(String(Math.round(v)), 0.034, '#cfe0f0');  // depth-tested
      lab.position.copy(p.clone().add(tick.clone().multiplyScalar(TICK * 2.6)));
      rulers.add(lab);
    }
  }
  if (withLabels) {
    const cap = makeLabel('height ' + U, 0.042, '#9fb3c8');
    cap.position.set(bx, vTop, bz);
    cap.position.add(tick.clone().multiplyScalar(TICK * 2.6));
    rulers.add(cap);
  }
  // Invisible cylinder as a fat hover target along this ruler.
  const hit = new THREE.Mesh(
    new THREE.CylinderGeometry(TICK * 1.1, TICK * 1.1, vTop, 6),
    new THREE.MeshBasicMaterial({ visible: false })
  );
  hit.position.set(bx, vTop / 2, bz);
  scene.add(hit);
  rulerHits.push(hit);
}
addHeightRuler(-1, -1, true);
addHeightRuler(1, -1, false);
addHeightRuler(-1, 1, false);
addHeightRuler(1, 1, false);

addGroundRuler(new THREE.Vector3(-PL / 2, 0.05, PW / 2 + OFF),
               new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 0, 1), PL, niceStep(PL), 'length ' + U);
addGroundRuler(new THREE.Vector3(-PL / 2 - OFF, 0.05, -PW / 2),
               new THREE.Vector3(0, 0, 1), new THREE.Vector3(-1, 0, 0), PW, niceStep(PW), 'width ' + U);

// Height-limit marker: dashed red loop at the max allowed height (always on top).
if (DATA.maxHeight && DATA.maxHeight > 0) {
  const mh = DATA.maxHeight;
  const loop = new THREE.BufferGeometry().setFromPoints([
    new THREE.Vector3(-PL/2, mh, -PW/2), new THREE.Vector3(PL/2, mh, -PW/2),
    new THREE.Vector3(PL/2, mh, PW/2), new THREE.Vector3(-PL/2, mh, PW/2),
    new THREE.Vector3(-PL/2, mh, -PW/2),
  ]);
  const limitLine = new THREE.Line(loop, new THREE.LineDashedMaterial({ color: 0xe06666, dashSize: 3, gapSize: 2 }));
  limitLine.computeLineDistances();
  rulers.add(limitLine);
  const lim = makeLabel('limit ' + Math.round(mh) + ' ' + U, 0.048, '#ff8a8a', true);
  lim.position.set(-PL / 2 - OFF, mh, PW / 2 + OFF);
  rulers.add(lim);
}

// Current-top indicator: a dashed green loop at the top of the boxes currently
// shown by the layer slider, with a height label that moves with it.
const curTopGeo = new THREE.BufferGeometry().setFromPoints([
  new THREE.Vector3(-PL/2, 0, -PW/2), new THREE.Vector3(PL/2, 0, -PW/2),
  new THREE.Vector3(PL/2, 0, PW/2), new THREE.Vector3(-PL/2, 0, PW/2),
  new THREE.Vector3(-PL/2, 0, -PW/2),
]);
const curTopLine = new THREE.Line(curTopGeo, new THREE.LineDashedMaterial({ color: 0x59d98b, dashSize: 3, gapSize: 1.5 }));
curTopLine.computeLineDistances();
curTopLine.renderOrder = 998;
scene.add(curTopLine);
let curTopLabel = null;
function updateCurrentTop(maxL) {
  let top = 0;
  boxMeshes.forEach((m) => {
    if (m.userData.layer <= maxL) {
      const t = m.userData.z + m.userData.h;
      if (t > top) top = t;
    }
  });
  curTopLine.position.y = top;
  if (curTopLabel) { scene.remove(curTopLabel); curTopLabel.material.map.dispose(); curTopLabel.material.dispose(); }
  curTopLabel = makeLabel('top ' + top.toFixed(1) + ' ' + U, 0.052, '#7be0a6', true);
  curTopLabel.position.set(PL / 2 + OFF, top, -PW / 2 - OFF);
  scene.add(curTopLabel);
}
const viewTop = Math.max(loadedH, vTop);
const home = new THREE.Vector3(PL * 1.0, viewTop + span * 0.8, PW * 1.2);
function resetCamera() {
  camera.position.copy(home);
  controls.target.set(0, viewTop / 2, 0);
  controls.update();
}
resetCamera();

// Controls: layer slider.
const slider = document.getElementById('layerSlider');
const layerVal = document.getElementById('layerVal');
slider.max = DATA.maxLayer;
slider.value = DATA.maxLayer;
layerVal.textContent = DATA.maxLayer;

function applyVisibility() {
  const maxL = parseInt(slider.value, 10);
  const solid = document.getElementById('solidChk').checked;
  const showLabels = document.getElementById('labelChk').checked;
  boxMeshes.forEach((m) => {
    const visible = m.userData.layer <= maxL;
    m.visible = visible;
    m.userData.edges.visible = visible;
    m.userData.label.visible = visible && showLabels;
    m.material.transparent = !solid;
    m.material.opacity = solid ? 1.0 : 0.45;
  });
  updateCurrentTop(maxL);
}
slider.addEventListener('input', () => { layerVal.textContent = slider.value; applyVisibility(); });
document.getElementById('solidChk').addEventListener('change', applyVisibility);
document.getElementById('labelChk').addEventListener('change', applyVisibility);
document.getElementById('resetBtn').addEventListener('click', resetCamera);
applyVisibility();

// Legend.
const legendEl = document.getElementById('legend');
legendEl.innerHTML = '<div style="font-weight:600;margin-bottom:4px;">Box types</div>' +
  DATA.legend.map((e) =>
    '<div class="item"><div class="sw" style="background:' + e.color + '"></div>' +
    '<div>' + e.name + ' &times;' + e.count + '</div></div>').join('');

// Hover tooltip via raycaster.
const raycaster = new THREE.Raycaster();
const mouse = new THREE.Vector2();
let hovered = null;
renderer.domElement.addEventListener('mousemove', (ev) => {
  const rect = renderer.domElement.getBoundingClientRect();
  mouse.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
  mouse.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(mouse, camera);
  const hits = raycaster.intersectObjects(boxMeshes.filter((m) => m.visible));
  if (hits.length) {
    const b = hits[0].object.userData;
    if (hovered && hovered !== hits[0].object) hovered.material.emissive.setHex(0x000000);
    hovered = hits[0].object;
    hovered.material.emissive.setHex(0x333333);
    const u = DATA.unit;
    tooltip.style.display = 'block';
    tooltip.style.left = (ev.clientX - rect.left + 12) + 'px';
    tooltip.style.top = (ev.clientY - rect.top + 12) + 'px';
    tooltip.innerHTML = '<b>' + b.name + '</b><br>' +
      b.l + ' x ' + b.w + ' x ' + b.h + ' ' + u + '<br>' +
      'layer ' + b.layer + ' · ' + b.weight + ' wt';
    return;
  }
  if (hovered) { hovered.material.emissive.setHex(0x000000); hovered = null; }
  // Not over a box: check the height rulers and report the height at the cursor.
  const rHits = raycaster.intersectObjects(rulerHits);
  if (rHits.length) {
    tooltip.style.display = 'block';
    tooltip.style.left = (ev.clientX - rect.left + 12) + 'px';
    tooltip.style.top = (ev.clientY - rect.top + 12) + 'px';
    tooltip.innerHTML = 'height: <b>' + rHits[0].point.y.toFixed(1) + ' ' + DATA.unit + '</b>';
  } else {
    tooltip.style.display = 'none';
  }
});

function onResize() {
  const w = wrap.clientWidth, h = wrap.clientHeight;
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
  renderer.setSize(w, h);
}
window.addEventListener('resize', onResize);

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();
</script>
</body>
</html>
"""
