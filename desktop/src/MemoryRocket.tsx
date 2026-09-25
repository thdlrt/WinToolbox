import './memoryRocket.css';

export default function MemoryRocket({ complete = false }: { complete?: boolean }) {
  return <div className={`memory-rocket ${complete ? 'memory-rocket-done' : ''}`} aria-hidden="true">
    <i className="rocket-star star-one"/><i className="rocket-star star-two"/><i className="rocket-star star-three"/>
    <svg viewBox="0 0 80 100"><defs><linearGradient id="rocketHull" x2="0" y2="1"><stop stopColor="#f4ffff"/><stop offset="1" stopColor="#8ce9dd"/></linearGradient></defs>
      <g className="rocket-flight"><path className="rocket-flame" d="M31 69 Q28 85 40 98 Q52 85 49 69"/><path fill="#45b9b2" d="M28 42 Q12 51 15 73 L29 66 M52 42 Q68 51 65 73 L51 66"/><path fill="url(#rocketHull)" d="M40 7 Q62 27 53 65 Q40 76 27 65 Q18 27 40 7"/><circle cx="40" cy="36" r="9" fill="#15394c" stroke="#fff" strokeWidth="3"/><path d="M29 64 Q40 70 51 64" fill="none" stroke="#239c9d" strokeWidth="3"/></g>
    </svg>
  </div>;
}
