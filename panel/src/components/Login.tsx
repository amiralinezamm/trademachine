import { useState } from 'react'
import { Eye, EyeOff } from 'lucide-react'
import { apiPost } from '../api/client'

interface Props {
  onLogin: () => void
}

const GoogleIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 48 48">
    <path fill="#FFC107" d="M43.611 20.083H42V20H24v8h11.303c-1.649 4.657-6.08 8-11.303 8-6.627 0-12-5.373-12-12s5.373-12 12-12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 12.955 4 4 12.955 4 24s8.955 20 20 20 20-8.955 20-20c0-2.641-.21-5.236-.611-7.743z" />
    <path fill="#FF3D00" d="M6.306 14.691l6.571 4.819C14.655 15.108 18.961 12 24 12c3.059 0 5.842 1.154 7.961 3.039l5.657-5.657C34.046 6.053 29.268 4 24 4 16.318 4 9.656 8.337 6.306 14.691z" />
    <path fill="#4CAF50" d="M24 44c5.166 0 9.86-1.977 13.409-5.192l-6.19-5.238C29.211 35.091 26.715 36 24 36c-5.202 0-9.619-3.317-11.283-7.946l-6.522 5.025C9.505 39.556 16.227 44 24 44z" />
    <path fill="#1976D2" d="M43.611 20.083H42V20H24v8h11.303c-.792 2.237-2.231 4.166-4.087 5.571l6.19 5.238C42.022 35.026 44 30.038 44 24c0-2.641-.21-5.236-.611-7.743z" />
  </svg>
)

const GlassInputWrapper = ({ children }: { children: React.ReactNode }) => (
  <div className="rounded-2xl border border-border bg-foreground/5 backdrop-blur-sm transition-colors focus-within:border-violet-400/70 focus-within:bg-violet-500/10">
    {children}
  </div>
)

export function Login({ onLogin }: Props) {
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  async function handleSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault()
    setError('')
    setLoading(true)
    const form = new FormData(e.currentTarget)
    const username = form.get('email') as string
    const password = form.get('password') as string
    try {
      await apiPost('/admin/api/login', { username, password })
      onLogin()
    } catch (err) {
      const e2 = err as Error & { status?: number }
      if (e2.status === 429) {
        setError('Too many attempts. Please wait 15 minutes.')
      } else {
        setError('Invalid credentials. Please try again.')
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="h-[100dvh] flex flex-col md:flex-row font-geist w-[100dvw]">
      {/* Left column: sign-in form */}
      <section className="flex-1 flex items-center justify-center p-8">
        <div className="w-full max-w-md">
          <div className="flex flex-col gap-6">
            <div>
              <h1 className="animate-element animate-delay-100 text-4xl md:text-5xl font-semibold leading-tight">
                <span className="font-light text-foreground tracking-tighter">Welcome back</span>
              </h1>
              <p className="animate-element animate-delay-200 text-muted-foreground mt-2">
                Sign in to your XAUUSD trading panel
              </p>
            </div>

            <form className="space-y-5" onSubmit={handleSubmit}>
              <div className="animate-element animate-delay-300">
                <label className="text-sm font-medium text-muted-foreground block mb-1.5">
                  Username
                </label>
                <GlassInputWrapper>
                  <input
                    name="email"
                    type="text"
                    placeholder="Enter your username"
                    autoComplete="username"
                    required
                    className="w-full bg-transparent text-sm p-4 rounded-2xl focus:outline-none text-foreground placeholder:text-muted-foreground/50"
                  />
                </GlassInputWrapper>
              </div>

              <div className="animate-element animate-delay-400">
                <label className="text-sm font-medium text-muted-foreground block mb-1.5">
                  Password
                </label>
                <GlassInputWrapper>
                  <div className="relative">
                    <input
                      name="password"
                      type={showPassword ? 'text' : 'password'}
                      placeholder="Enter your password"
                      autoComplete="current-password"
                      required
                      className="w-full bg-transparent text-sm p-4 pr-12 rounded-2xl focus:outline-none text-foreground placeholder:text-muted-foreground/50"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="absolute inset-y-0 right-3 flex items-center"
                    >
                      {showPassword
                        ? <EyeOff className="w-5 h-5 text-muted-foreground hover:text-foreground transition-colors" />
                        : <Eye className="w-5 h-5 text-muted-foreground hover:text-foreground transition-colors" />
                      }
                    </button>
                  </div>
                </GlassInputWrapper>
              </div>

              <div className="animate-element animate-delay-500 flex items-center justify-between text-sm">
                <label className="flex items-center gap-3 cursor-pointer select-none">
                  <input type="checkbox" name="rememberMe" className="custom-checkbox" />
                  <span className="text-foreground/90">Keep me signed in</span>
                </label>
              </div>

              {error && (
                <p className="animate-element text-sm text-red-400 text-center -mt-1">
                  {error}
                </p>
              )}

              <button
                type="submit"
                disabled={loading}
                className="animate-element animate-delay-600 w-full rounded-2xl bg-primary py-4 font-medium text-primary-foreground hover:bg-primary/90 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
              >
                {loading ? 'Signing in...' : 'Sign In'}
              </button>
            </form>

            {/* Divider */}
            <div className="animate-element animate-delay-700 relative flex items-center justify-center">
              <span className="w-full border-t border-border"></span>
              <span className="px-4 text-sm text-muted-foreground bg-background absolute whitespace-nowrap">
                Secure admin access
              </span>
            </div>

            {/* Footer note */}
            <p className="animate-element animate-delay-800 text-center text-sm text-muted-foreground">
              XAUUSD Trading Bot — Admin Panel
            </p>
          </div>
        </div>
      </section>

      {/* Right column: decorative */}
      <section className="hidden md:flex flex-1 relative p-4 items-center justify-center overflow-hidden">
        {/* Dark gradient background matching chart theme */}
        <div className="animate-slide-right animate-delay-300 absolute inset-4 rounded-3xl overflow-hidden"
          style={{ background: 'linear-gradient(135deg, #1e222d 0%, #131722 50%, #0d1117 100%)' }}>
          {/* Decorative gold glow */}
          <div style={{
            position: 'absolute', top: '30%', left: '50%',
            transform: 'translate(-50%, -50%)',
            width: 320, height: 320,
            background: 'radial-gradient(circle, rgba(255,183,77,0.08) 0%, transparent 70%)',
            pointerEvents: 'none',
          }} />
          {/* Decorative price lines */}
          <svg style={{ position: 'absolute', inset: 0, opacity: 0.15 }} width="100%" height="100%">
            {[20, 35, 50, 62, 75].map((y, i) => (
              <line key={i} x1="0" y1={`${y}%`} x2="100%" y2={`${y}%`}
                stroke="#d1d4dc" strokeWidth="0.5" strokeDasharray={i % 2 === 0 ? '4 8' : '2 12'} />
            ))}
          </svg>
          {/* Center label */}
          <div style={{
            position: 'absolute', top: '50%', left: '50%',
            transform: 'translate(-50%, -50%)',
            textAlign: 'center',
          }}>
            <div style={{ fontSize: '3rem', fontWeight: 700, color: '#d1d4dc', letterSpacing: '-2px', lineHeight: 1 }}>
              XAU
            </div>
            <div style={{ fontSize: '1rem', color: '#787b86', marginTop: 8, letterSpacing: '4px' }}>
              USD
            </div>
            <div style={{ marginTop: 16, display: 'flex', gap: 8, justifyContent: 'center' }}>
              <span style={{ padding: '4px 12px', borderRadius: 99, background: 'rgba(38,166,154,0.15)', color: '#26a69a', fontSize: '0.75rem', border: '1px solid rgba(38,166,154,0.3)' }}>
                Signals
              </span>
              <span style={{ padding: '4px 12px', borderRadius: 99, background: 'rgba(41,98,255,0.15)', color: '#2962ff', fontSize: '0.75rem', border: '1px solid rgba(41,98,255,0.3)' }}>
                Live
              </span>
            </div>
          </div>
        </div>
      </section>
    </div>
  )
}
