/**
 * 录音时的波浪指示：5 根竖条起伏。纯展示，动画在 index.css 的 `voice-wave` keyframes 里。
 */
const BARS = [0, 1, 2, 3, 4]

export default function WaveAnimation() {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 4, height: 24 }}>
      {BARS.map((index) => (
        <span
          key={index}
          style={{
            display: 'inline-block',
            width: 4,
            height: '100%',
            borderRadius: 2,
            background: '#1677ff',
            animation: 'voice-wave 0.9s ease-in-out infinite',
            // 每根错开相位，才有起伏的错落感。
            animationDelay: `${index * 0.12}s`,
          }}
        />
      ))}
    </div>
  )
}
