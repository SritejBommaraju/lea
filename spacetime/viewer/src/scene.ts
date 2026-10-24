// The 3D view: the scan's coloured LiDAR cloud or mesh (PLY, arkit_world metres, Y up) with a pin per located finding.
import * as THREE from 'three';
import { PLYLoader } from 'three/examples/jsm/loaders/PLYLoader.js';

export type Pin = { id: string; x: number; y: number; z: number; tone: string };

const TONES: Record<string, number> = { safety: 0xff5c66, functional: 0xffb340, cosmetic: 0xa4b0c4, '': 0x8c83ff };

function ringTexture(): THREE.Texture {
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  g.strokeStyle = '#fff';
  g.lineWidth = 10;
  g.beginPath();
  g.arc(64, 64, 52, 0, Math.PI * 2);
  g.stroke();
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

export class ScanScene {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(32, 1, 0.01, 50);
  private group = new THREE.Group();
  /** The scan's own frame (arkit_world metres), moved so the object's centre is at the origin. The model and the
   *  pins are both children, with the pins at their raw arkit_world points: one frame and one centring for both, so
   *  a reload with another centre (cloud or mesh, a new revision) moves them together. */
  private content = new THREE.Group();
  private object: THREE.Object3D | null = null;
  private pins = new THREE.Group();
  private pinData: Pin[] = [];
  private offset = new THREE.Vector3();
  private radius = 0.2;
  private ring = ringTexture();
  private loadedUrl = '';
  private loading: Promise<boolean> | null = null;
  private t0 = performance.now();

  constructor(private canvas: HTMLCanvasElement) {
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x222233, 1.6));
    const key = new THREE.DirectionalLight(0xffffff, 1.4);
    key.position.set(1, 2, 1.5);
    this.scene.add(key);
    this.content.add(this.pins);
    this.group.add(this.content);
    this.scene.add(this.group);
    new ResizeObserver(() => this.resize()).observe(canvas.parentElement!);
    this.resize();
    const tick = () => {
      this.frame();
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }

  get hasObject(): boolean {
    return this.object !== null;
  }

  private resize(): void {
    const el = this.canvas.parentElement!;
    const w = el.clientWidth, h = el.clientHeight;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / Math.max(1, h);
    this.camera.updateProjectionMatrix();
  }

  /** Load a PLY (once per URL and rev). `points` draws it as a coloured cloud, else as a vertex-coloured mesh. The
   *  signing parameters change with every refresh and are ignored; `rev` changes when more photos rebuild the scan. */
  load(url: string, points: boolean): Promise<boolean> {
    const parsed = new URL(url, window.location.href);
    const key = parsed.origin + parsed.pathname + '|rev=' + (parsed.searchParams.get('rev') ?? '') + '|' + points;
    if (key === this.loadedUrl) return this.loading ?? Promise.resolve(true);
    this.loadedUrl = key;
    this.loading = new PLYLoader().loadAsync(url).then((geo) => {
      geo.computeBoundingBox();
      const box = geo.boundingBox!;
      box.getCenter(this.offset);
      geo.computeBoundingSphere();
      this.radius = Math.max(0.02, geo.boundingSphere!.radius);
      this.content.position.set(-this.offset.x, -this.offset.y, -this.offset.z);
      let obj: THREE.Object3D;
      if (points || !geo.index) {
        obj = new THREE.Points(geo, new THREE.PointsMaterial({
          size: this.radius * 0.018, vertexColors: geo.hasAttribute('color'), sizeAttenuation: true,
          color: geo.hasAttribute('color') ? 0xffffff : 0x8c83ff,
        }));
      } else {
        geo.computeVertexNormals();
        obj = new THREE.Mesh(geo, new THREE.MeshStandardMaterial({
          vertexColors: geo.hasAttribute('color'), roughness: 0.85, metalness: 0.0, side: THREE.DoubleSide,
        }));
      }
      if (this.object) {
        this.content.remove(this.object);
        disposeObject(this.object);
      }
      this.object = obj;
      this.content.add(obj);
      this.rebuildPins();
      this.camera.position.set(0, this.radius * 1.0, this.radius * 2.9);
      this.camera.lookAt(0, 0, 0);
      this.t0 = performance.now();
      return true;
    }).catch((e) => {
      console.warn('3D load failed', e);
      this.loadedUrl = '';
      return false;
    });
    return this.loading;
  }

  clear(): void {
    if (this.object) {
      this.content.remove(this.object);
      disposeObject(this.object);
      this.object = null;
    }
    this.loadedUrl = '';
    this.setPins([]);
  }

  /** Pins sized for the model now loaded (its radius): made again when another model loads. */
  private rebuildPins(): void {
    const keep = this.pinData;
    for (const child of [...this.pins.children]) {
      this.pins.remove(child);
      disposeObject(child);
    }
    this.pinData = [];
    this.setPins(keep);
  }

  setPins(pins: Pin[]): void {
    this.pinData = pins;
    const want = new Set(pins.map((p) => p.id));
    for (const child of [...this.pins.children]) {
      if (!want.has(child.name)) {
        this.pins.remove(child);
        disposeObject(child);
      }
    }
    for (const p of pins) {
      if (this.pins.getObjectByName(p.id)) continue;
      const color = TONES[p.tone] ?? TONES[''];
      const pin = new THREE.Group();
      pin.name = p.id;
      pin.position.set(p.x, p.y, p.z); // arkit_world, like the model's vertices (content holds the centring)
      const dot = new THREE.Mesh(
        new THREE.SphereGeometry(this.radius * 0.03, 24, 16),
        new THREE.MeshBasicMaterial({ color, depthTest: false, transparent: true }),
      );
      dot.renderOrder = 10;
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.ring, color, depthTest: false, transparent: true }));
      halo.renderOrder = 11;
      halo.userData.halo = true;
      pin.add(dot, halo);
      pin.userData.born = performance.now();
      this.pins.add(pin);
    }
  }

  private frame(): void {
    const now = performance.now();
    this.group.rotation.y = ((now - this.t0) / 1000) * 0.22;
    for (const pin of this.pins.children) {
      const age = (now - (pin.userData.born as number)) / 1000;
      const grow = Math.min(1, age / 0.6);
      pin.scale.setScalar(1 - Math.pow(1 - grow, 3));
      for (const c of pin.children) {
        if (!c.userData.halo) continue;
        const phase = (age % 1.6) / 1.6;
        c.scale.setScalar(this.radius * (0.08 + 0.12 * phase));
        (c as THREE.Sprite).material.opacity = 0.9 * (1 - phase);
      }
    }
    this.renderer.render(this.scene, this.camera);
  }
}

function disposeObject(o: THREE.Object3D): void {
  o.traverse((x) => {
    const m = x as THREE.Mesh;
    m.geometry?.dispose?.();
    const mat = m.material as THREE.Material | THREE.Material[] | undefined;
    if (Array.isArray(mat)) mat.forEach((y) => y.dispose());
    else mat?.dispose?.();
  });
}
