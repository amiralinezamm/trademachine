import { motion } from 'motion/react'

const DOTS = ['', '.', '..', '...']

export function AITextLoading({ text = 'در حال بارگذاری' }: { text?: string }) {
  return (
    <div style={{
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      width: '100%',
      height: '100%',
      gap: 16,
    }}>
      <motion.div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          fontSize: '0.9rem',
          color: '#d1d4dc',
          fontFamily: 'inherit',
        }}
      >
        <motion.span
          style={{
            background: 'linear-gradient(90deg, #787b86 0%, #d1d4dc 40%, #2962ff 60%, #787b86 100%)',
            backgroundSize: '200% 100%',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
            backgroundClip: 'text',
          }}
          animate={{ backgroundPosition: ['200% center', '-200% center'] }}
          transition={{ duration: 2, repeat: Infinity, ease: 'linear' }}
        >
          {text}
        </motion.span>
        <DotsAnimation />
      </motion.div>
      <div style={{ display: 'flex', gap: 6 }}>
        {[0, 1, 2].map((i) => (
          <motion.div
            key={i}
            style={{
              width: 6,
              height: 6,
              borderRadius: '50%',
              background: '#2962ff',
            }}
            animate={{ opacity: [0.2, 1, 0.2], scale: [0.8, 1, 0.8] }}
            transition={{
              duration: 1.2,
              repeat: Infinity,
              delay: i * 0.2,
              ease: 'easeInOut',
            }}
          />
        ))}
      </div>
    </div>
  )
}

function DotsAnimation() {
  return (
    <motion.span
      style={{ color: '#787b86', minWidth: 20, display: 'inline-block' }}
      animate={{ opacity: [1, 1, 1, 1] }}
      transition={{ duration: 1.2, repeat: Infinity }}
    >
      <motion.span
        animate={{ opacity: [0, 1, 1, 1, 0] }}
        transition={{ duration: 1.2, repeat: Infinity, times: [0, 0.33, 0.66, 0.99, 1] }}
      >
        .
      </motion.span>
      <motion.span
        animate={{ opacity: [0, 0, 1, 1, 0] }}
        transition={{ duration: 1.2, repeat: Infinity, times: [0, 0.33, 0.66, 0.99, 1] }}
      >
        .
      </motion.span>
      <motion.span
        animate={{ opacity: [0, 0, 0, 1, 0] }}
        transition={{ duration: 1.2, repeat: Infinity, times: [0, 0.33, 0.66, 0.99, 1] }}
      >
        .
      </motion.span>
    </motion.span>
  )
}
