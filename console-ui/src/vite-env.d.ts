/// <reference types="vite/client" />

declare module "three" {
  export class WebGLRenderer {
    constructor(parameters?: any);
    domElement: HTMLCanvasElement;
    setPixelRatio(value: number): void;
    setSize(width: number, height: number, updateStyle?: boolean): void;
    setClearColor(color: any, alpha?: number): void;
    render(scene: any, camera: any): void;
    dispose(): void;
    toneMapping: any;
    toneMappingExposure: number;
    [key: string]: any;
  }

  export class Scene {
    constructor();
    add(...object: any[]): this;
    remove(...object: any[]): this;
    children: any[];
    traverse(callback: (object: any) => any): void;
    [key: string]: any;
  }

  export class Camera {
    position: Vector3;
    quaternion: any;
    rotation: any;
    lookAt(x: number | Vector3, y?: number, z?: number): void;
    [key: string]: any;
  }

  export class PerspectiveCamera extends Camera {
    constructor(fov?: number, aspect?: number, near?: number, far?: number);
    aspect: number;
    fov: number;
    near: number;
    far: number;
    updateProjectionMatrix(): void;
    [key: string]: any;
  }

  export class Vector3 {
    constructor(x?: number, y?: number, z?: number);
    x: number;
    y: number;
    z: number;
    set(x: number, y: number, z: number): this;
    copy(v: Vector3): this;
    clone(): Vector3;
    add(v: Vector3): this;
    sub(v: Vector3): this;
    lerpVectors(v1: Vector3, v2: Vector3, alpha: number): this;
    distanceTo(v: Vector3): number;
    length(): number;
    normalize(): this;
    multiplyScalar(scalar: number): this;
    [key: string]: any;
  }

  export class Vector2 {
    constructor(x?: number, y?: number);
    x: number;
    y: number;
    set(x: number, y: number): this;
    copy(v: Vector2): this;
    clone(): Vector2;
    [key: string]: any;
  }

  export class Raycaster {
    constructor(origin?: Vector3, direction?: Vector3, near?: number, far?: number);
    setFromCamera(coords: Vector2, camera: Camera): void;
    intersectObjects(objects: any[], recursive?: boolean, optionalTarget?: any[]): any[];
    intersectObject(object: any, recursive?: boolean, optionalTarget?: any[]): any[];
    [key: string]: any;
  }

  export class Color {
    constructor(color?: any);
    set(color: any): this;
    setHex(hex: number): this;
    getHex(): number;
    getHexString(): string;
    [key: string]: any;
  }

  export class Object3D {
    position: Vector3;
    rotation: any;
    quaternion: any;
    scale: Vector3;
    userData: Record<string, any>;
    visible: boolean;
    parent: Object3D | null;
    children: Object3D[];
    add(...object: Object3D[]): this;
    remove(...object: Object3D[]): this;
    [key: string]: any;
  }

  export class Group extends Object3D {
    constructor();
  }

  export class Mesh extends Object3D {
    constructor(geometry?: any, material?: any);
    geometry: any;
    material: any;
  }

  export class Sprite extends Object3D {
    constructor(material?: any);
    material: any;
  }

  export class Points extends Object3D {
    constructor(geometry?: any, material?: any);
    geometry: any;
    material: any;
  }

  export class Line extends Object3D {
    constructor(geometry?: any, material?: any);
    geometry: any;
    material: any;
  }

  export class LineSegments extends Line {
    constructor(geometry?: any, material?: any);
  }

  export class BufferGeometry {
    constructor();
    setAttribute(name: string, attribute: any): this;
    getAttribute(name: string): any;
    dispose(): void;
    [key: string]: any;
  }

  export class SphereGeometry extends BufferGeometry {
    constructor(radius?: number, widthSegments?: number, heightSegments?: number, phiStart?: number, phiLength?: number, thetaStart?: number, thetaLength?: number);
  }

  export class RingGeometry extends BufferGeometry {
    constructor(innerRadius?: number, outerRadius?: number, thetaSegments?: number, phiSegments?: number, thetaStart?: number, thetaLength?: number);
  }

  export class TorusGeometry extends BufferGeometry {
    constructor(radius?: number, tube?: number, radialSegments?: number, tubularSegments?: number, arc?: number);
  }

  export class BufferAttribute {
    constructor(array: ArrayLike<number>, itemSize: number, normalized?: boolean);
    array: ArrayLike<number>;
    itemSize: number;
    count: number;
    needsUpdate: boolean;
    [key: string]: any;
  }

  export class Float32BufferAttribute extends BufferAttribute {
    constructor(array: ArrayLike<number> | ArrayBuffer, itemSize: number, normalized?: boolean);
  }

  export class Material {
    constructor();
    opacity: number;
    transparent: boolean;
    visible: boolean;
    blending: any;
    side: any;
    depthWrite: boolean;
    dispose(): void;
    clone(): this;
    [key: string]: any;
  }

  export class MeshBasicMaterial extends Material {
    constructor(parameters?: any);
    color: Color;
    wireframe: boolean;
    [key: string]: any;
  }

  export class MeshStandardMaterial extends Material {
    constructor(parameters?: any);
    color: Color;
    emissive: Color;
    emissiveIntensity: number;
    roughness: number;
    metalness: number;
    [key: string]: any;
  }

  export class LineBasicMaterial extends Material {
    constructor(parameters?: any);
    color: Color;
    linewidth: number;
    [key: string]: any;
  }

  export class PointsMaterial extends Material {
    constructor(parameters?: any);
    color: Color;
    size: number;
    sizeAttenuation: boolean;
    map: Texture | null;
    [key: string]: any;
  }

  export class SpriteMaterial extends Material {
    constructor(parameters?: any);
    color: Color;
    map: Texture | null;
    [key: string]: any;
  }

  export class Texture {
    constructor(image?: any);
    dispose(): void;
    colorSpace: string;
    needsUpdate: boolean;
    [key: string]: any;
  }

  export class CanvasTexture extends Texture {
    constructor(canvas: HTMLCanvasElement);
  }

  export class AmbientLight extends Object3D {
    constructor(color?: any, intensity?: number);
  }

  export class DirectionalLight extends Object3D {
    constructor(color?: any, intensity?: number);
  }

  export class PointLight extends Object3D {
    constructor(color?: any, intensity?: number, distance?: number, decay?: number);
  }

  export const AdditiveBlending: any;
  export const NormalBlending: any;
  export const BackSide: any;
  export const DoubleSide: any;
  export const FrontSide: any;
  export const SRGBColorSpace: any;
  export const ACESFilmicToneMapping: any;
  export type ColorRepresentation = string | number | Color;
}

declare module "three/addons/*" {
  export const OrbitControls: any;
  const content: any;
  export default content;
}

declare module "three/addons/controls/OrbitControls.js" {
  import { Camera } from "three";
  export class OrbitControls {
    constructor(camera: Camera, domElement?: HTMLElement);
    enabled: boolean;
    target: any;
    enableDamping: boolean;
    dampingFactor: number;
    enableZoom: boolean;
    screenSpacePanning: boolean;
    minDistance: number;
    maxDistance: number;
    maxPolarAngle: number;
    minPolarAngle: number;
    autoRotate: boolean;
    autoRotateSpeed: number;
    update(): boolean;
    dispose(): void;
    addEventListener(type: string, listener: (event: any) => void): void;
    removeEventListener(type: string, listener: (event: any) => void): void;
  }
}

declare module "three/examples/jsm/*" {
  export const OrbitControls: any;
  const content: any;
  export default content;
}

declare module "three/examples/jsm/controls/OrbitControls.js" {
  import { Camera } from "three";
  export class OrbitControls {
    constructor(camera: Camera, domElement?: HTMLElement);
    enabled: boolean;
    target: any;
    enableDamping: boolean;
    dampingFactor: number;
    enableZoom: boolean;
    screenSpacePanning: boolean;
    minDistance: number;
    maxDistance: number;
    maxPolarAngle: number;
    minPolarAngle: number;
    autoRotate: boolean;
    autoRotateSpeed: number;
    update(): boolean;
    dispose(): void;
    addEventListener(type: string, listener: (event: any) => void): void;
    removeEventListener(type: string, listener: (event: any) => void): void;
  }
}
