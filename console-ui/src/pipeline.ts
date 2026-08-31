import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import {
  CanonicalStage,
  CANONICAL_STAGES,
  formatUsd,
  loadPipeline,
  PipelineCounts,
  PipelineLead,
  PipelinePayload,
  STAGE_CONFIG,
} from "./api";

/* =========================================================================
   Helper Utilities
   ========================================================================= */

function escapeHtml(str: string | null | undefined): string {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function easeInOutCubic(t: number): number {
  return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
}

/* =========================================================================
   3D Pipeline Engine
   ========================================================================= */

export class PipelineEngine {
  private canvas: HTMLCanvasElement;
  private renderer: THREE.WebGLRenderer;
  private scene: THREE.Scene;
  private camera: THREE.PerspectiveCamera;
  private controls: OrbitControls;

  // Starfield & Scene Groups
  private starTexture: THREE.Texture;
  private starfieldGroup: THREE.Group;
  private wellGroups: Map<CanonicalStage, THREE.Group> = new Map();
  private wellRings: Map<
    CanonicalStage,
    { ring1: THREE.Mesh; ring2: THREE.Mesh; core: THREE.Mesh }
  > = new Map();
  private constellationLines: Map<CanonicalStage, THREE.LineSegments> = new Map();

  // Nodes & Selection
  private leadMeshes: Map<string, THREE.Mesh> = new Map();
  private sharedNodeGeo = new THREE.SphereGeometry(1.0, 18, 18);
  private sharedMaterials: Map<CanonicalStage, THREE.MeshStandardMaterial> = new Map();

  // Raycasting & Interaction
  private raycaster = new THREE.Raycaster();
  private pointer = new THREE.Vector2();
  private isPointerDown = false;
  private pointerDownPos = { x: 0, y: 0 };
  private pointerDownTime = 0;
  private hoveredLeadId: string | null = null;
  private selectedLeadId: string | null = null;

  // Camera Animation
  private isTransitioning = false;
  private transitionStart = 0;
  private transitionDuration = 1200;
  private startCamPos = new THREE.Vector3();
  private targetCamPos = new THREE.Vector3();
  private startTarget = new THREE.Vector3();
  private targetTarget = new THREE.Vector3();

  // Lifecycle
  private animationFrameId = 0;
  private pollIntervalId = 0;
  private clockIntervalId = 0;
  private isDestroyed = false;

  // Cached Pipeline Data
  private currentData: PipelinePayload | null = null;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;

    // 1. WebGL Renderer
    this.renderer = new THREE.WebGLRenderer({
      canvas: this.canvas,
      antialias: true,
      alpha: false,
      powerPreference: "high-performance",
    });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    this.renderer.setSize(window.innerWidth, window.innerHeight, false);
    this.renderer.setClearColor(0x050403, 1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    this.renderer.toneMappingExposure = 1.15;

    // 2. Scene & Camera
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(
      52,
      window.innerWidth / window.innerHeight,
      0.1,
      1000
    );
    this.camera.position.set(0, 48, 140);

    // 3. OrbitControls
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.05;
    this.controls.screenSpacePanning = false;
    this.controls.minDistance = 15;
    this.controls.maxDistance = 400;
    this.controls.maxPolarAngle = Math.PI / 2 + 0.35;
    this.controls.target.set(0, 5, 0);
    this.controls.update();

    // Abort camera animation on user interaction
    this.controls.addEventListener("start", () => {
      if (this.isTransitioning) {
        this.isTransitioning = false;
      }
    });

    // 4. Lighting
    const ambientLight = new THREE.AmbientLight(0xffffff, 0.7);
    const dirLight = new THREE.DirectionalLight(0x00f0ff, 0.9);
    dirLight.position.set(30, 80, 50);
    this.scene.add(ambientLight, dirLight);

    // 5. Starfield & Sprite Textures
    this.starTexture = this.createStarSprite();
    this.starfieldGroup = this.buildStarfield();
    this.scene.add(this.starfieldGroup);

    // 6. Gravity Wells & Materials
    this.buildGravityWells();

    // 7. Event Handlers
    this.initEventListeners();
    this.initUiListeners();

    // 8. Start Loops
    this.animate(0);
    this.startClock();
    this.startPolling();
  }

  /* =========================================================================
     Starfield & Texture Generation
     ========================================================================= */

  private createStarSprite(): THREE.Texture {
    const size = 64;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const ctx = canvas.getContext("2d");
    if (!ctx) return new THREE.Texture();

    const glow = ctx.createRadialGradient(32, 32, 0, 32, 32, 32);
    glow.addColorStop(0, "rgba(255, 255, 255, 1.0)");
    glow.addColorStop(0.12, "rgba(255, 255, 255, 0.9)");
    glow.addColorStop(0.28, "rgba(255, 255, 255, 0.35)");
    glow.addColorStop(0.65, "rgba(255, 255, 255, 0.05)");
    glow.addColorStop(1.0, "rgba(255, 255, 255, 0)");

    ctx.fillStyle = glow;
    ctx.fillRect(0, 0, size, size);

    const texture = new THREE.CanvasTexture(canvas);
    texture.colorSpace = THREE.SRGBColorSpace;
    return texture;
  }

  private buildStarfield(): THREE.Group {
    const group = new THREE.Group();

    const createField = (
      positions: Float32Array,
      size: number,
      colorHex: number,
      opacity = 0.9
    ) => {
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(positions, 3));
      const mat = new THREE.PointsMaterial({
        size,
        map: this.starTexture,
        color: new THREE.Color(colorHex),
        transparent: true,
        depthWrite: false,
        blending: THREE.AdditiveBlending,
        sizeAttenuation: true,
        opacity,
      });
      return new THREE.Points(geo, mat);
    };

    // 1. Far sphere field (9000 stars)
    const farCount = 9000;
    const farPos = new Float32Array(farCount * 3);
    for (let i = 0; i < farCount; i++) {
      const r = 380 * (0.35 + Math.random() * 0.65);
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      farPos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      farPos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      farPos[i * 3 + 2] = r * Math.cos(phi);
    }
    const farPoints = createField(farPos, 0.45, 0x94a3b8, 0.85);

    // 2. Galactic disc band (4500 stars)
    const bandCount = 4500;
    const bandPos = new Float32Array(bandCount * 3);
    for (let i = 0; i < bandCount; i++) {
      const r = 290 * Math.sqrt(Math.random());
      const theta = Math.random() * Math.PI * 2;
      bandPos[i * 3] = r * Math.cos(theta);
      bandPos[i * 3 + 1] = (Math.random() - 0.5) * 40;
      bandPos[i * 3 + 2] = r * Math.sin(theta);
    }
    const bandPoints = createField(bandPos, 0.65, 0x38bdf8, 0.7);
    bandPoints.rotation.x = 0.45;
    bandPoints.rotation.z = 0.2;

    // 3. Near shimmer stars (350 stars)
    const nearCount = 350;
    const nearPos = new Float32Array(nearCount * 3);
    for (let i = 0; i < nearCount; i++) {
      const r = 120 * (0.2 + Math.random() * 0.8);
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      nearPos[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      nearPos[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta);
      nearPos[i * 3 + 2] = r * Math.cos(phi);
    }
    const nearPoints = createField(nearPos, 1.2, 0xc0ff70, 0.9);

    group.add(farPoints, bandPoints, nearPoints);
    return group;
  }

  /* =========================================================================
     Gravity Wells (Constellation Cores)
     ========================================================================= */

  private buildGravityWells(): void {
    CANONICAL_STAGES.forEach((stage) => {
      const cfg = STAGE_CONFIG[stage];
      const wellGroup = new THREE.Group();
      wellGroup.position.set(...cfg.anchor);

      // Core glowing sphere
      const coreGeo = new THREE.SphereGeometry(2.4, 28, 28);
      const coreMat = new THREE.MeshBasicMaterial({
        color: cfg.colorHex,
        transparent: true,
        opacity: 0.92,
      });
      const coreMesh = new THREE.Mesh(coreGeo, coreMat);

      // Aura shell with additive BackSide glow
      const auraGeo = new THREE.SphereGeometry(3.8, 20, 20);
      const auraMat = new THREE.MeshBasicMaterial({
        color: cfg.colorHex,
        transparent: true,
        opacity: 0.25,
        blending: THREE.AdditiveBlending,
        side: THREE.BackSide,
      });
      const auraMesh = new THREE.Mesh(auraGeo, auraMat);

      // Inner Pulsar Ring
      const ring1Geo = new THREE.TorusGeometry(4.8, 0.08, 12, 48);
      const ring1Mat = new THREE.MeshBasicMaterial({
        color: cfg.colorHex,
        wireframe: true,
        transparent: true,
        opacity: 0.65,
        blending: THREE.AdditiveBlending,
      });
      const ring1 = new THREE.Mesh(ring1Geo, ring1Mat);
      ring1.rotation.x = Math.PI / 3.5;

      // Outer Counter-Rotating Ring
      const ring2Geo = new THREE.TorusGeometry(6.4, 0.06, 12, 48);
      const ring2Mat = new THREE.MeshBasicMaterial({
        color: cfg.colorHex,
        wireframe: true,
        transparent: true,
        opacity: 0.45,
        blending: THREE.AdditiveBlending,
      });
      const ring2 = new THREE.Mesh(ring2Geo, ring2Mat);
      ring2.rotation.y = Math.PI / 2.8;

      // Point Light
      const pointLight = new THREE.PointLight(cfg.colorHex, 2.2, 70);

      wellGroup.add(coreMesh, auraMesh, ring1, ring2, pointLight);
      this.scene.add(wellGroup);

      this.wellGroups.set(stage, wellGroup);
      this.wellRings.set(stage, { ring1, ring2, core: coreMesh });

      // Create reusable standard material for nodes of this stage
      const nodeMat = new THREE.MeshStandardMaterial({
        color: cfg.colorHex,
        emissive: new THREE.Color(cfg.colorHex),
        emissiveIntensity: 0.75,
        roughness: 0.2,
        metalness: 0.6,
      });
      this.sharedMaterials.set(stage, nodeMat);
    });
  }

  /* =========================================================================
     Data Update & Graph Diffing Engine
     ========================================================================= */

  public updateData(data: PipelinePayload): void {
    this.currentData = data;
    const existingIds = new Set(this.leadMeshes.keys());
    const incomingIds = new Set<string>();

    CANONICAL_STAGES.forEach((stage) => {
      const leads = data.grouped[stage] || [];
      const wellGroup = this.wellGroups.get(stage);
      if (!wellGroup) return;

      const cfg = STAGE_CONFIG[stage];
      const count = leads.length;

      leads.forEach((lead, idx) => {
        incomingIds.add(lead.id);

        let mesh = this.leadMeshes.get(lead.id);
        const radius = 8.5 + ((idx * 2.2) % 16.0);
        const theta = Math.acos(1 - (2 * (idx + 0.5)) / Math.max(count, 1));
        const phi = Math.PI * (1 + Math.sqrt(5)) * idx;

        const lx = radius * Math.sin(theta) * Math.cos(phi);
        const ly = radius * Math.cos(theta) * 0.55;
        const lz = radius * Math.sin(theta) * Math.sin(phi);

        const payoutBonus = Math.min(lead.projected_payout_usd / 2500, 1.25);
        const baseScale = 0.85 + payoutBonus;

        if (!mesh) {
          // Create new bounty lead node
          const mat =
            this.sharedMaterials.get(stage) ||
            this.sharedMaterials.get("queued")!;
          mesh = new THREE.Mesh(this.sharedNodeGeo, mat.clone());

          // Sprite Glow Halo
          const spriteMat = new THREE.SpriteMaterial({
            map: this.starTexture,
            color: new THREE.Color(cfg.colorHex),
            transparent: true,
            blending: THREE.AdditiveBlending,
            opacity: 0.65,
          });
          const sprite = new THREE.Sprite(spriteMat);
          sprite.scale.set(3.2, 3.2, 1.0);
          mesh.add(sprite);

          wellGroup.add(mesh);
          this.leadMeshes.set(lead.id, mesh);
        } else if (mesh.parent !== wellGroup) {
          // Lead moved stages: re-parent to new Gravity Well
          mesh.parent?.remove(mesh);
          wellGroup.add(mesh);
        }

        mesh.position.set(lx, ly, lz);
        mesh.scale.set(baseScale, baseScale, baseScale);
        mesh.userData = {
          isLeadNode: true,
          lead,
          stage,
          baseScale,
          initialTheta: theta,
          initialPhi: phi,
          orbitalRadius: radius,
          orbitalSpeed: 0.12 / Math.sqrt(radius),
        };
      });

      // Update Constellation Lines for this Gravity Well
      this.updateClusterLines(stage, leads);
    });

    // Remove defunct nodes
    existingIds.forEach((id) => {
      if (!incomingIds.has(id)) {
        const mesh = this.leadMeshes.get(id);
        if (mesh) {
          mesh.parent?.remove(mesh);
          if (mesh.material && typeof mesh.material.dispose === "function") {
            mesh.material.dispose();
          }
          this.leadMeshes.delete(id);
        }
      }
    });

    // Update UI counters and badges
    this.updateUiCounts(data.counts, data.leads);

    // If currently selected lead was updated, re-render HUD
    if (this.selectedLeadId) {
      const selectedLead = data.leads.find((l) => l.id === this.selectedLeadId);
      if (selectedLead) {
        this.renderHud(selectedLead);
      } else {
        this.renderHud(null);
      }
    }
  }

  private updateClusterLines(stage: CanonicalStage, leads: PipelineLead[]): void {
    const wellGroup = this.wellGroups.get(stage);
    if (!wellGroup) return;

    // Dispose previous lines
    const oldLines = this.constellationLines.get(stage);
    if (oldLines) {
      wellGroup.remove(oldLines);
      oldLines.geometry.dispose();
      (oldLines.material as THREE.Material).dispose();
      this.constellationLines.delete(stage);
    }

    if (leads.length === 0) return;

    const points: number[] = [];
    const nodePositions: THREE.Vector3[] = [];

    leads.forEach((lead) => {
      const mesh = this.leadMeshes.get(lead.id);
      if (mesh) {
        nodePositions.push(mesh.position.clone());
        // Line from well core (0,0,0) to node
        points.push(0, 0, 0, mesh.position.x, mesh.position.y, mesh.position.z);
      }
    });

    // Inter-node connections for nearest neighbors
    for (let i = 0; i < nodePositions.length; i++) {
      for (let j = i + 1; j < nodePositions.length; j++) {
        const dist = nodePositions[i].distanceTo(nodePositions[j]);
        if (dist < 13.5) {
          points.push(
            nodePositions[i].x,
            nodePositions[i].y,
            nodePositions[i].z,
            nodePositions[j].x,
            nodePositions[j].y,
            nodePositions[j].z
          );
        }
      }
    }

    const lineGeo = new THREE.BufferGeometry();
    lineGeo.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(points, 3)
    );
    const lineMat = new THREE.LineBasicMaterial({
      color: new THREE.Color(STAGE_CONFIG[stage].colorHex),
      transparent: true,
      opacity: 0.32,
      blending: THREE.AdditiveBlending,
    });
    const lines = new THREE.LineSegments(lineGeo, lineMat);
    wellGroup.add(lines);
    this.constellationLines.set(stage, lines);
  }

  /* =========================================================================
     Camera Navigation & Smooth Fly-To
     ========================================================================= */

  public flyToStage(stageKey: CanonicalStage | "all"): void {
    this.isTransitioning = true;
    this.transitionStart = performance.now();
    this.startCamPos.copy(this.camera.position);
    this.startTarget.copy(this.controls.target);

    if (stageKey === "all" || !STAGE_CONFIG[stageKey]) {
      this.targetCamPos.set(0, 48, 140);
      this.targetTarget.set(0, 5, 0);
    } else {
      const cfg = STAGE_CONFIG[stageKey];
      this.targetCamPos.set(...cfg.camPos);
      this.targetTarget.set(...cfg.anchor);
    }

    // Update active stage chip UI
    document.querySelectorAll(".stage-chip").forEach((chip) => {
      const chipStage = chip.getAttribute("data-stage");
      chip.classList.toggle("active", chipStage === stageKey);
    });
  }

  public flyToNode(lead: PipelineLead): void {
    const mesh = this.leadMeshes.get(lead.id);
    if (!mesh) return;

    const worldPos = new THREE.Vector3();
    mesh.getWorldPosition(worldPos);

    this.isTransitioning = true;
    this.transitionStart = performance.now();
    this.startCamPos.copy(this.camera.position);
    this.startTarget.copy(this.controls.target);

    // Position camera 18 units away along current viewing vector
    const offset = this.camera.position.clone().sub(worldPos).normalize();
    if (offset.length() < 0.1) offset.set(0, 0.5, 1).normalize();
    this.targetCamPos.copy(worldPos).add(offset.multiplyScalar(18));
    this.targetTarget.copy(worldPos);
  }

  /* =========================================================================
     Event Listeners & Pointer Interactions
     ========================================================================= */

  private initEventListeners(): void {
    window.addEventListener("resize", this.onResize);
    this.canvas.addEventListener("pointermove", this.onPointerMove, {
      passive: true,
    });
    this.canvas.addEventListener("pointerdown", this.onPointerDown);
    this.canvas.addEventListener("pointerup", this.onPointerUp);
  }

  private onResize = (): void => {
    const width = window.innerWidth;
    const height = window.innerHeight;
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  };

  private onPointerMove = (e: PointerEvent): void => {
    const rect = this.canvas.getBoundingClientRect();
    this.pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this.pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    this.raycaster.setFromCamera(this.pointer, this.camera);
    const meshes = Array.from(this.leadMeshes.values());
    const intersects = this.raycaster.intersectObjects(meshes, false);

    if (intersects.length > 0) {
      const hitMesh = intersects[0].object as THREE.Mesh;
      const lead = hitMesh.userData.lead as PipelineLead;
      this.canvas.style.cursor = "pointer";

      if (this.hoveredLeadId !== lead.id) {
        this.hoveredLeadId = lead.id;
      }
      this.renderTooltip(lead, e.clientX, e.clientY);
    } else {
      this.canvas.style.cursor = "default";
      if (this.hoveredLeadId !== null) {
        this.hoveredLeadId = null;
      }
      this.renderTooltip(null, 0, 0);
    }
  };

  private onPointerDown = (e: PointerEvent): void => {
    this.isPointerDown = true;
    this.pointerDownPos = { x: e.clientX, y: e.clientY };
    this.pointerDownTime = performance.now();
  };

  private onPointerUp = (e: PointerEvent): void => {
    if (!this.isPointerDown) return;
    this.isPointerDown = false;

    // Distinguish clean click from drag
    const dist = Math.hypot(
      e.clientX - this.pointerDownPos.x,
      e.clientY - this.pointerDownPos.y
    );
    const duration = performance.now() - this.pointerDownTime;
    if (dist > 6 || duration > 350) return;

    const rect = this.canvas.getBoundingClientRect();
    this.pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
    this.pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

    this.raycaster.setFromCamera(this.pointer, this.camera);
    const meshes = Array.from(this.leadMeshes.values());
    const intersects = this.raycaster.intersectObjects(meshes, false);

    if (intersects.length > 0) {
      const hitMesh = intersects[0].object as THREE.Mesh;
      const lead = hitMesh.userData.lead as PipelineLead;
      this.selectedLeadId = lead.id;
      this.renderHud(lead);
      this.flyToNode(lead);
    } else {
      // Clicked empty space: do not close HUD immediately to avoid accidental dismissal during orbit,
      // but if click was deliberate on background without dragging, clear selection
      if (dist < 2) {
        this.selectedLeadId = null;
        this.renderHud(null);
      }
    }
  };

  /* =========================================================================
     HUD, Tooltip & DOM UI Integration
     ========================================================================= */

  private initUiListeners(): void {
    // Stage Filter Buttons
    const stageChips = document.getElementById("stage-chips");
    if (stageChips) {
      stageChips.addEventListener("click", (e) => {
        const target = (e.target as HTMLElement).closest(".stage-chip");
        if (!target) return;
        const stage = target.getAttribute("data-stage") as CanonicalStage | "all";
        if (stage) {
          this.flyToStage(stage);
        }
      });
    }

    // Reset View Button
    const resetBtn = document.getElementById("btn-reset-view");
    if (resetBtn) {
      resetBtn.addEventListener("click", () => {
        this.selectedLeadId = null;
        this.renderHud(null);
        this.flyToStage("all");
      });
    }

    // HUD Close Button
    const hudCloseBtn = document.getElementById("hud-close-btn");
    if (hudCloseBtn) {
      hudCloseBtn.addEventListener("click", () => {
        this.selectedLeadId = null;
        this.renderHud(null);
      });
    }
  }

  private renderTooltip(
    lead: PipelineLead | null,
    clientX: number,
    clientY: number
  ): void {
    const tooltip = document.getElementById("pipeline-tooltip");
    if (!tooltip) return;

    if (!lead) {
      tooltip.classList.remove("visible");
      tooltip.setAttribute("aria-hidden", "true");
      return;
    }

    const badge = document.getElementById("tooltip-status-badge");
    const payout = document.getElementById("tooltip-payout");
    const repo = document.getElementById("tooltip-repo");
    const title = document.getElementById("tooltip-title");

    if (badge) {
      badge.textContent = lead.status.replace("_", " ").toUpperCase();
      badge.className = `tooltip-badge ${lead.status}`;
    }
    if (payout) {
      payout.textContent = lead.projected_payout || formatUsd(lead.projected_payout_usd);
    }
    if (repo) {
      repo.textContent = `${lead.repo}${lead.issue_number ? ` #${lead.issue_number}` : ""}`;
    }
    if (title) {
      title.textContent = lead.title || "Untitled Bounty Lead";
    }

    // Clamp coordinates within window bounds
    const tooltipWidth = 260;
    const tooltipHeight = 90;
    const posX = Math.min(clientX + 14, window.innerWidth - tooltipWidth - 12);
    const posY = Math.min(clientY + 14, window.innerHeight - tooltipHeight - 12);

    tooltip.style.left = `${posX}px`;
    tooltip.style.top = `${posY}px`;
    tooltip.classList.add("visible");
    tooltip.setAttribute("aria-hidden", "false");
  }

  private renderHud(lead: PipelineLead | null): void {
    const hud = document.getElementById("pipeline-hud");
    const hudContent = document.getElementById("hud-content");
    if (!hud || !hudContent) return;

    if (!lead) {
      hud.classList.add("empty");
      hudContent.innerHTML = `
        <div class="hud-placeholder">
          <div class="hud-placeholder-icon">✦</div>
          <h3>Select a Gravity Node</h3>
          <p>Hover or click any bounty star in the constellation graph to inspect lead parameters, escrow guarantees, and PR telemetry.</p>
        </div>
      `;
      return;
    }

    hud.classList.remove("empty");

    const stageLabel = lead.status.replace("_", " ").toUpperCase();
    const escrowClass = lead.escrow_verified ? "" : "unverified";
    const escrowLabel = lead.escrow_verified ? "Escrow Verified" : "Unfunded / Pending";
    const payoutFormatted = lead.projected_payout || formatUsd(lead.projected_payout_usd);
    const usdValue = formatUsd(lead.projected_payout_usd);

    hudContent.innerHTML = `
      <div class="hud-lead-header">
        <span class="hud-stage-badge ${lead.status}">${escapeHtml(stageLabel)}</span>
        <span class="hud-escrow-badge ${escrowClass}">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>
          </svg>
          <span>${escapeHtml(escrowLabel)}</span>
        </span>
      </div>

      <h2 class="hud-lead-title">${escapeHtml(lead.title || "Untitled Bounty Lead")}</h2>

      <div class="hud-payout-hero">
        <div class="hud-payout-label">Projected Reward</div>
        <div class="hud-payout-amount">${escapeHtml(payoutFormatted)}</div>
      </div>

      <dl class="hud-grid">
        <dt>Repository</dt>
        <dd><a href="https://github.com/${escapeHtml(lead.repo)}" target="_blank" rel="noopener noreferrer" style="color: var(--volt); text-decoration: none;">${escapeHtml(lead.repo)}</a></dd>
        <dt>Issue</dt>
        <dd>${lead.issue_number ? `#${lead.issue_number}` : "—"}</dd>
        <dt>USD Value</dt>
        <dd>${escapeHtml(usdValue)}</dd>
        <dt>Ecosystem</dt>
        <dd>${escapeHtml(lead.ecosystem || "General")}</dd>
        <dt>Strategy</dt>
        <dd>${escapeHtml(lead.qualification_reason || "Standard discovery & intake pipeline")}</dd>
      </dl>

      <div class="hud-actions">
        ${
          lead.pr_url
            ? `<a class="hud-link-btn primary" href="${escapeHtml(lead.pr_url)}" target="_blank" rel="noopener noreferrer">
                <span>View Pull Request</span>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
                  <polyline points="15 3 21 3 21 9"/>
                  <line x1="10" y1="14" x2="21" y2="3"/>
                </svg>
              </a>`
            : ""
        }
        ${
          lead.issue_url
            ? `<a class="hud-link-btn secondary" href="${escapeHtml(lead.issue_url)}" target="_blank" rel="noopener noreferrer">
                <span>Open GitHub Issue</span>
              </a>`
            : ""
        }
        <button id="hud-focus-btn" class="hud-link-btn secondary" type="button">
          <span>Center Camera On Node</span>
        </button>
      </div>
    `;

    const focusBtn = document.getElementById("hud-focus-btn");
    if (focusBtn) {
      focusBtn.addEventListener("click", () => {
        this.flyToNode(lead);
      });
    }
  }

  private updateUiCounts(counts: PipelineCounts, leads: PipelineLead[]): void {
    const totalEl = document.getElementById("pipeline-total-badge");
    if (totalEl) {
      totalEl.textContent = `${counts.total} LEADS`;
    }

    const countAll = document.getElementById("count-all");
    if (countAll) countAll.textContent = String(counts.total);

    const countQueued = document.getElementById("count-queued");
    if (countQueued) countQueued.textContent = String(counts.queued);

    const countTriage = document.getElementById("count-pending_triage");
    if (countTriage) countTriage.textContent = String(counts.pending_triage);

    const countPr = document.getElementById("count-pr_open");
    if (countPr) countPr.textContent = String(counts.pr_open);

    const countComp = document.getElementById("count-completed");
    if (countComp) countComp.textContent = String(counts.completed);

    const countFail = document.getElementById("count-failed");
    if (countFail) countFail.textContent = String(counts.failed);

    // Calculate Total Pipeline Value
    const totalUsd = leads.reduce((sum, item) => sum + (item.projected_payout_usd || 0), 0);
    const totalValEl = document.getElementById("total-pipeline-value");
    if (totalValEl) {
      totalValEl.textContent = formatUsd(totalUsd);
    }

    // Last Sync timestamp
    const syncEl = document.getElementById("last-sync-time");
    if (syncEl) {
      const now = new Date();
      syncEl.textContent = `Last sync: ${now.toISOString().substring(11, 19)}Z`;
    }
  }

  /* =========================================================================
     Polling & Clocks
     ========================================================================= */

  private startClock(): void {
    const updateClock = () => {
      const clockEl = document.getElementById("clock");
      if (clockEl) {
        const now = new Date();
        clockEl.textContent = `${now.toISOString().substring(11, 19)}Z`;
      }
    };
    updateClock();
    this.clockIntervalId = window.setInterval(updateClock, 1000);
  }

  private startPolling(): void {
    const poll = async () => {
      if (this.isDestroyed) return;
      try {
        const payload = await loadPipeline();
        this.updateData(payload);
        const healthDot = document.getElementById("health-dot");
        const healthLabel = document.getElementById("health-label");
        if (healthDot) healthDot.className = "dot ok";
        if (healthLabel) healthLabel.textContent = "live";
      } catch (err) {
        console.warn("Pipeline poll failed:", err);
        const healthDot = document.getElementById("health-dot");
        const healthLabel = document.getElementById("health-label");
        if (healthDot) healthDot.className = "dot bad";
        if (healthLabel) healthLabel.textContent = "offline";
      }
    };

    poll();
    this.pollIntervalId = window.setInterval(poll, 5000);
  }

  /* =========================================================================
     Animation Loop & Render
     ========================================================================= */

  private animate = (timestamp: number): void => {
    if (this.isDestroyed) return;
    this.animationFrameId = requestAnimationFrame(this.animate);
    const time = timestamp * 0.001;

    // 1. Rotate Starfield
    this.starfieldGroup.rotation.y = time * 0.02;

    // 2. Animate Gravity Well pulsar rings & glowing core opacity
    this.wellRings.forEach(({ ring1, ring2, core }) => {
      ring1.rotation.z += 0.008;
      ring2.rotation.y -= 0.006;
      const pulse = 1.0 + 0.06 * Math.sin(time * 2.5);
      ring1.scale.set(pulse, pulse, pulse);
      ring2.scale.set(pulse, pulse, pulse);

      const coreMat = core.material as THREE.MeshBasicMaterial;
      coreMat.opacity = 0.8 + 0.15 * Math.sin(time * 3.0);
    });

    // 3. Orbital drift of lead nodes
    this.leadMeshes.forEach((mesh) => {
      const {
        baseScale,
        initialTheta,
        initialPhi,
        orbitalRadius,
        orbitalSpeed,
        lead,
      } = mesh.userData;
      if (orbitalRadius) {
        const phi = initialPhi + time * orbitalSpeed;
        mesh.position.x = orbitalRadius * Math.sin(initialTheta) * Math.cos(phi);
        mesh.position.z = orbitalRadius * Math.sin(initialTheta) * Math.sin(phi);

        const isHovered = lead?.id === this.hoveredLeadId;
        const isSelected = lead?.id === this.selectedLeadId;
        const scaleMult = isSelected ? 1.45 : isHovered ? 1.25 : 1.0;
        const targetScale = baseScale * scaleMult;
        mesh.scale.set(targetScale, targetScale, targetScale);
      }
    });

    // 4. Camera Fly-To Interpolation
    if (this.isTransitioning) {
      const elapsed = performance.now() - this.transitionStart;
      const t = Math.min(elapsed / this.transitionDuration, 1.0);
      const ease = easeInOutCubic(t);

      this.camera.position.lerpVectors(this.startCamPos, this.targetCamPos, ease);
      this.controls.target.lerpVectors(this.startTarget, this.targetTarget, ease);

      if (t >= 1.0) {
        this.isTransitioning = false;
      }
    }

    this.controls.update();
    this.renderer.render(this.scene, this.camera);
  };

  /* =========================================================================
     Cleanup & Teardown
     ========================================================================= */

  public destroy(): void {
    this.isDestroyed = true;
    cancelAnimationFrame(this.animationFrameId);
    window.clearInterval(this.pollIntervalId);
    window.clearInterval(this.clockIntervalId);

    window.removeEventListener("resize", this.onResize);
    this.canvas.removeEventListener("pointermove", this.onPointerMove);
    this.canvas.removeEventListener("pointerdown", this.onPointerDown);
    this.canvas.removeEventListener("pointerup", this.onPointerUp);

    this.leadMeshes.forEach((mesh) => {
      mesh.parent?.remove(mesh);
      if (mesh.material && typeof mesh.material.dispose === "function") {
        mesh.material.dispose();
      }
    });
    this.leadMeshes.clear();

    this.constellationLines.forEach((lines) => {
      lines.geometry.dispose();
      (lines.material as THREE.Material).dispose();
    });
    this.constellationLines.clear();

    this.sharedMaterials.forEach((mat) => mat.dispose());
    this.sharedMaterials.clear();
    this.sharedNodeGeo.dispose();
    this.starTexture.dispose();

    this.controls.dispose();
    this.renderer.dispose();
  }
}

/* =========================================================================
   Bootstrap Entry Point
   ========================================================================= */

document.addEventListener("DOMContentLoaded", () => {
  const canvas = document.getElementById("pipeline-canvas") as HTMLCanvasElement;
  if (!canvas) {
    console.error("Canvas #pipeline-canvas not found in DOM");
    return;
  }

  const engine = new PipelineEngine(canvas);
  (window as any).__pipelineEngine = engine;
});
