import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { animate } from "animejs";
import { Activity, Radio, RotateCw } from "lucide-react";
import type { Service, ServiceStatus } from "../types";

type NodeRecord = {
  group: THREE.Group;
  core: THREE.Mesh;
  halo: THREE.Mesh;
  ring: THREE.Mesh;
  position: THREE.Vector3;
};

const colors: Record<ServiceStatus, number> = { live: 0xb4dd6b, building: 0xf0a45d, stopped: 0x687489, failed: 0xee7b81 };

export default function ServiceTopology({ services, selectedId, onSelect, connected }: { services: Service[]; selectedId?: string; onSelect: (service: Service) => void; connected: boolean }) {
  const mountRef = useRef<HTMLDivElement>(null);
  const sceneRef = useRef<{ renderer: THREE.WebGLRenderer; camera: THREE.PerspectiveCamera; scene: THREE.Scene; nodes: Map<string, NodeRecord>; raycaster: THREE.Raycaster; pointer: THREE.Vector2; frame: number } | null>(null);
  const servicesRef = useRef(services);
  const [hovered, setHovered] = useState<string | null>(null);
  servicesRef.current = services;

  useEffect(() => {
    if (!mountRef.current) return;
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(44, 1, 0.1, 100);
    camera.position.set(0, 0.3, 6.4);
    const renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);
    mountRef.current.appendChild(renderer.domElement);

    const world = new THREE.Group();
    scene.add(world);
    const nodes = new Map<string, NodeRecord>();
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2(-10, -10);
    sceneRef.current = { renderer, camera, scene, nodes, raycaster, pointer, frame: 0 };

    const ambient = new THREE.AmbientLight(0x9ab6dd, 1.2);
    scene.add(ambient);
    const hub = new THREE.Mesh(new THREE.IcosahedronGeometry(.32, 1), new THREE.MeshBasicMaterial({ color: 0x62d8dd, wireframe: true, transparent: true, opacity: .9 }));
    world.add(hub);
    const hubGlow = new THREE.Mesh(new THREE.SphereGeometry(.58, 24, 24), new THREE.MeshBasicMaterial({ color: 0x62d8dd, transparent: true, opacity: .055 }));
    world.add(hubGlow);

    const starGeometry = new THREE.BufferGeometry();
    const starPositions = new Float32Array(450 * 3);
    for (let i = 0; i < starPositions.length; i += 3) { starPositions[i] = (Math.random() - .5) * 9; starPositions[i + 1] = (Math.random() - .5) * 5; starPositions[i + 2] = (Math.random() - .5) * 3; }
    starGeometry.setAttribute("position", new THREE.BufferAttribute(starPositions, 3));
    const stars = new THREE.Points(starGeometry, new THREE.PointsMaterial({ color: 0x7896bd, size: .018, transparent: true, opacity: .42 }));
    world.add(stars);

    const resize = () => {
      if (!mountRef.current || !sceneRef.current) return;
      const { width, height } = mountRef.current.getBoundingClientRect();
      renderer.setSize(width, height, false);
      camera.aspect = width / Math.max(height, 1);
      camera.updateProjectionMatrix();
    };
    const onPointerMove = (event: PointerEvent) => {
      if (!mountRef.current || !sceneRef.current) return;
      const bounds = renderer.domElement.getBoundingClientRect();
      pointer.x = ((event.clientX - bounds.left) / bounds.width) * 2 - 1;
      pointer.y = -((event.clientY - bounds.top) / bounds.height) * 2 + 1;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects([...nodes.values()].map((node) => node.core));
      const next = hit[0]?.object.userData.serviceId as string | undefined;
      setHovered(next ?? null);
      renderer.domElement.style.cursor = next ? "pointer" : "default";
    };
    const onClick = () => {
      if (!sceneRef.current) return;
      raycaster.setFromCamera(pointer, camera);
      const hit = raycaster.intersectObjects([...nodes.values()].map((node) => node.core));
      const id = hit[0]?.object.userData.serviceId as string | undefined;
      const service = servicesRef.current.find((item) => item.id === id);
      if (service) onSelect(service);
    };
    resize();
    window.addEventListener("resize", resize);
    renderer.domElement.addEventListener("pointermove", onPointerMove);
    renderer.domElement.addEventListener("click", onClick);
    const render = () => {
      if (!sceneRef.current) return;
      sceneRef.current.frame = requestAnimationFrame(render);
      const time = performance.now() * .001;
      world.rotation.y = Math.sin(time * .16) * .07;
      world.rotation.x = Math.sin(time * .1) * .018;
      hub.rotation.x += .006;
      hub.rotation.y += .009;
      hubGlow.scale.setScalar(1 + Math.sin(time * 2.4) * .07);
      for (const node of nodes.values()) {
        node.ring.rotation.z += .007;
        node.halo.scale.setScalar(1 + Math.sin(time * 2.2 + node.position.x) * .08);
      }
      renderer.render(scene, camera);
    };
    render();

    return () => {
      if (sceneRef.current) cancelAnimationFrame(sceneRef.current.frame);
      window.removeEventListener("resize", resize);
      renderer.domElement.removeEventListener("pointermove", onPointerMove);
      renderer.domElement.removeEventListener("click", onClick);
      starGeometry.dispose();
      renderer.dispose();
      renderer.domElement.remove();
      sceneRef.current = null;
    };
  }, [onSelect]);

  useEffect(() => {
    const state = sceneRef.current;
    if (!state) return;
    const { scene, nodes } = state;
    const liveServices = services.filter((service) => service.status !== "stopped");
    const visibleIds = new Set(liveServices.map((service) => service.id));
    const lines = scene.getObjectByName("topology-lines");
    if (lines) scene.remove(lines);
    const lineGroup = new THREE.Group();
    lineGroup.name = "topology-lines";
    scene.add(lineGroup);
    const lineMaterial = new THREE.LineBasicMaterial({ color: 0x49647e, transparent: true, opacity: .42 });

    services.forEach((service, index) => {
      const existing = nodes.get(service.id);
      if (!visibleIds.has(service.id)) {
        if (existing) { existing.group.visible = false; }
        return;
      }
      const angle = (index / Math.max(liveServices.length, 1)) * Math.PI * 2 - Math.PI / 2;
      const radius = liveServices.length <= 1 ? 0 : liveServices.length === 2 ? 1.58 : 1.8;
      const position = new THREE.Vector3(Math.cos(angle) * radius, Math.sin(angle) * radius * .56, Math.sin(angle) * .3);
      const color = colors[service.status];
      let node = existing;
      if (!node) {
        const group = new THREE.Group();
        const core = new THREE.Mesh(new THREE.SphereGeometry(.115, 20, 20), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .98 }));
        core.userData.serviceId = service.id;
        const halo = new THREE.Mesh(new THREE.SphereGeometry(.24, 20, 20), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .11 }));
        const ring = new THREE.Mesh(new THREE.TorusGeometry(.22, .012, 8, 40), new THREE.MeshBasicMaterial({ color, transparent: true, opacity: .64 }));
        group.add(halo, ring, core);
        group.position.copy(position);
        scene.add(group);
        node = { group, core, halo, ring, position };
        nodes.set(service.id, node);
        group.scale.setScalar(.72);
        animate(group.scale, { x: [.72, 1], y: [.72, 1], z: [.72, 1], duration: 620, ease: "outExpo" });
      }
      node.group.visible = true;
      node.position.copy(position);
      node.group.position.copy(position);
      const material = node.core.material as THREE.MeshBasicMaterial;
      material.color.setHex(color);
      (node.halo.material as THREE.MeshBasicMaterial).color.setHex(color);
      (node.ring.material as THREE.MeshBasicMaterial).color.setHex(color);
      const lineGeometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(0, 0, 0), position]);
      lineGroup.add(new THREE.Line(lineGeometry, lineMaterial));
    });
    for (const [id, node] of nodes) {
      if (!visibleIds.has(id)) node.group.visible = false;
    }
    return () => {
      lineGroup.children.forEach((child) => { const line = child as THREE.Line; line.geometry.dispose(); });
      lineMaterial.dispose();
    };
  }, [services]);

  useEffect(() => {
    const state = sceneRef.current;
    if (!state) return;
    for (const service of services) {
      const node = state.nodes.get(service.id);
      if (!node) continue;
      const active = selectedId === service.id || hovered === service.id;
      animate(node.group.scale, { x: active ? 1.35 : 1, y: active ? 1.35 : 1, z: active ? 1.35 : 1, duration: 240, ease: "outQuad" });
      animate(node.ring.rotation, { z: active ? node.ring.rotation.z + Math.PI * 2 : node.ring.rotation.z + .1, duration: active ? 520 : 160, ease: "outExpo" });
    }
  }, [selectedId, hovered, services]);

  const activeCount = services.filter((service) => service.status === "live").length;
  return <section className="topology-panel animate-in"><div className="topology-head"><div><div className="section-kicker"><Activity size={12} /> LIVE INFRASTRUCTURE</div><h2>Service topology <span>·</span> <em>{activeCount} active nodes</em></h2></div><div className={`stream-state ${connected ? "connected" : "reconnecting"}`}><Radio size={12} /> {connected ? "STREAM CONNECTED" : "RECONNECTING"}</div></div><div className="topology-stage"><div ref={mountRef} className="topology-canvas" /><div className="topology-hub-label"><span className="hub-orbit" /><strong>HOSTING BOT</strong><small>CONTROL PLANE</small></div>{services.filter((service) => service.status !== "stopped").map((service) => { const node = sceneRef.current?.nodes.get(service.id); if (!node) return null; return <button key={service.id} className={`node-label ${selectedId === service.id ? "selected" : ""}`} style={{ left: `${50 + node.position.x * 13}%`, top: `${50 - node.position.y * 23}%` }} onClick={() => onSelect(service)}><span className="node-status" style={{ background: `#${colors[service.status].toString(16).padStart(6, "0")}` }} /><span>{service.name}</span><small>{service.status === "building" ? "deploying" : `${service.cpu}% cpu`}</small></button>; })}<div className="topology-legend"><span><i className="legend-dot live" />Live</span><span><i className="legend-dot building" />Building</span><span><i className="legend-dot stopped" />Paused</span></div><div className="topology-footer"><span><RotateCw size={12} /> Updates are pushed instantly from the control plane</span><span className="mono">SSE /api/events</span></div></div></section>;
}
