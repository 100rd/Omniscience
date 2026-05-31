import { useEffect, useRef } from "react";

interface Node {
  id: string;
  name: string;
  kind: string;
  x?: number;
  y?: number;
}

interface Link {
  source: string;
  target: string;
  type: string;
}

interface Props {
  nodes: Node[];
  links: Link[];
  onNodeClick: (name: string) => void;
}

export function GraphCanvas({ nodes, links, onNodeClick }: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Simple Force-Directed Simulation (Internal Implementation)
    let animationFrame: number;
    const width = canvas.width;
    const height = canvas.height;

    // Initial positions
    nodes.forEach(n => {
      n.x = Math.random() * width;
      n.y = Math.random() * height;
    });

    const runFrame = () => {
      ctx.clearRect(0, 0, width, height);

      // 1. Draw Links
      ctx.strokeStyle = "#e2e8f0";
      ctx.lineWidth = 1;
      links.forEach(l => {
        const s = nodes.find(n => n.id === l.source);
        const t = nodes.find(n => n.id === l.target);
        if (s && t && s.x && s.y && t.x && t.y) {
          ctx.beginPath();
          ctx.moveTo(s.x, s.y);
          ctx.lineTo(t.x, t.y);
          ctx.stroke();
        }
      });

      // 2. Draw Nodes
      nodes.forEach(n => {
        if (!n.x || !n.y) return;
        
        ctx.fillStyle = n.kind === "service" ? "#3b82f6" : "#6366f1";
        ctx.beginPath();
        ctx.arc(n.x, n.y, 6, 0, Math.PI * 2);
        ctx.fill();

        ctx.fillStyle = "#1e293b";
        ctx.font = "10px sans-serif";
        ctx.fillText(n.name.split('.').pop() || n.name, n.x + 10, n.y + 3);
      });

      // Simple attraction/repulsion logic
      nodes.forEach(n1 => {
        nodes.forEach(n2 => {
          if (n1 === n2) return;
          const dx = n2.x! - n1.x!;
          const dy = n2.y! - n1.y!;
          const dist = Math.sqrt(dx*dx + dy*dy) || 1;
          const force = 0.5 / (dist * dist);
          n1.x! -= dx * force * 100;
          n1.y! -= dy * force * 100;
        });
        
        // Attraction to center
        n1.x! += (width / 2 - n1.x!) * 0.01;
        n1.y! += (height / 2 - n1.y!) * 0.01;
      });

      animationFrame = requestAnimationFrame(runFrame);
    };

    runFrame();
    return () => cancelAnimationFrame(animationFrame);
  }, [nodes, links]);

  return (
    <canvas 
      ref={canvasRef} 
      width={800} 
      height={600} 
      className="w-full h-[600px] bg-gray-50 rounded-lg border border-gray-200 cursor-crosshair"
      onClick={(e) => {
        // Basic node selection by proximity
        const rect = canvasRef.current?.getBoundingClientRect();
        if (!rect) return;
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const hit = nodes.find(n => Math.abs(n.x! - x) < 15 && Math.abs(n.y! - y) < 15);
        if (hit) onNodeClick(hit.name);
      }}
    />
  );
}
