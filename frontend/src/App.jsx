import { useState, useRef, useEffect } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://localhost:8000'

function MessageContent({ text }) {
  if (!text) return null

  const lines = text.split('\n')
  const elements = []
  let listItems = []

  const flushList = () => {
    if (listItems.length > 0) {
      elements.push(
        <ul key={`list-${elements.length}`} className="response-list">
          {listItems.map((item, i) => (
            <li key={i}>{item}</li>
          ))}
        </ul>
      )
      listItems = []
    }
  }

  lines.forEach((line) => {
    const trimmed = line.trim()

    if (trimmed.startsWith('* ')) {
      listItems.push(formatText(trimmed.slice(2)))
    } else {
      flushList()
      if (trimmed) {
        elements.push(
          <p key={`p-${elements.length}`} className="response-paragraph">
            {formatText(trimmed)}
          </p>
        )
      }
    }
  })

  flushList()
  return <div className="message-content">{elements}</div>
}

function formatText(str) {
  return str
    .split(/(\*\*.*?\*\*|\[.*?\]\(.*?\))/g)
    .map((part, i) => {
      if (part.startsWith('**')) {
        return <strong key={i}>{part.slice(2, -2)}</strong>
      }
      if (part.startsWith('[')) {
        const match = part.match(/\[(.*?)\]\((.*?)\)/)
        if (match) {
          return (
            <a key={i} href={match[2]} target="_blank" rel="noopener noreferrer" className="response-link">
              {match[1]}
            </a>
          )
        }
      }
      return part
    })
}

function App() {
  const [messages, setMessages] = useState([])
  const [input, setInput] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [userId, setUserId] = useState('')
  const messagesEndRef = useRef(null)

  useEffect(() => {
    const savedId = localStorage.getItem('ira_chat_user_id')
    const id = savedId || `user-${Date.now()}-${Math.random().toString(36).slice(2)}`
    if (!savedId) {
      localStorage.setItem('ira_chat_user_id', id)
    }
    setUserId(id)
  }, [])

  useEffect(() => {
    if (!userId) return

    const loadHistory = async () => {
      try {
        const response = await fetch(`${API_BASE}/history?user_id=${encodeURIComponent(userId)}&limit=20`)
        if (!response.ok) {
          throw new Error('Could not load conversation history.')
        }
        const data = await response.json()
        if (data.messages.length > 0) {
          setMessages(data.messages)
        } else {
          setMessages([
            {
              role: 'assistant',
              text: "Hello! I'm your IRA Skills policy assistant. Ask me about Terms of Service, Privacy Policy, refunds, contact details, or blog information.",
            },
          ])
        }
      } catch (err) {
        setMessages([
          {
            role: 'assistant',
            text: "Hello! I'm your IRA Skills policy assistant. Ask me about Terms of Service, Privacy Policy, refunds, contact details, or blog information.",
          },
        ])
      }
    }

    loadHistory()
  }, [userId])

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  const sendMessage = async () => {
    const question = input.trim()
    if (!question) {
      setError('Please enter a question before sending.')
      return
    }

    setError('')
    setLoading(true)
    const userMessage = { role: 'user', text: question }
    setMessages((prev) => [...prev, userMessage])
    setInput('')

    try {
      const response = await fetch(`${API_BASE}/chat`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question, user_id: userId }),
      })

      if (!response.ok) {
        throw new Error(`Request failed: ${response.status}`)
      }

      const data = await response.json()
      setMessages((prev) => [
        ...prev,
        { role: 'assistant', text: data.answer, sources: data.sources || [] },
      ])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Unable to send the question.')
      setMessages((prev) => [...prev, { role: 'assistant', text: 'An error occurred. Please try again.' }])
    } finally {
      setLoading(false)
    }
  }

  const handleSubmit = (event) => {
    event.preventDefault()
    sendMessage()
  }

  return (
    <div className="chat-app">
      <header className="chat-header">
        <div>
          <p className="app-tag">IRA Skills RAG Chatbot</p>
          <h1>Policy Assistant</h1>
          <p className="app-description">
            Ask questions about IRA Skills policies, and I will answer using the knowledge base.
          </p>
        </div>
      </header>

      <main className="chat-panel">
        <div className="message-list" aria-live="polite">
          {messages.map((message, index) => (
            <article
              key={`${message.role}-${index}`}
              className={`message ${message.role === 'assistant' ? 'assistant' : 'user'}`}
            >
              <div className="message-bubble">
                <div className="message-meta">
                  <span>{message.role === 'assistant' ? 'Assistant' : 'You'}</span>
                </div>
                <MessageContent text={message.text} />
                
              </div>
            </article>
          ))}
          <div ref={messagesEndRef} />
        </div>

        <form className="chat-form" onSubmit={handleSubmit}>
          <textarea
            id="question"
            rows="3"
            value={input}
            onChange={(event) => setInput(event.target.value)}
            placeholder="Type your question here..."
            disabled={loading}
          />
          <div className="form-actions">
            <button type="submit" disabled={loading}>
              {loading ? 'Thinking...' : 'Send question'}
            </button>
          </div>
        </form>

        {error && <div className="chat-error">{error}</div>}
      </main>
    </div>
  )
}

export default App
