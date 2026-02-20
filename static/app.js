function fmtTime(s){
  const m = Math.floor(s/60).toString().padStart(2,'0')
  const r = (s%60).toString().padStart(2,'0')
  return `${m}:${r}`
}

// INDEX: formulário principal
window.addEventListener('DOMContentLoaded', () => {
  const form = document.querySelector('#form-start')
  if (!form) return

  const statusCard = document.querySelector('#status')
  const progressBar = document.querySelector('#progress-bar')
  const logEl = document.querySelector('#log')
  const elapsedEl = document.querySelector('#elapsed')
  const etaEl = document.querySelector('#eta')
  const goto = document.querySelector('#goto')
  const link = document.querySelector('#results-link')
  const useRefs = document.querySelector('#use_refs')
  const refInput = document.querySelector('#ref_images')

  useRefs?.addEventListener('change', () => {
    refInput.disabled = !useRefs.checked
  })

  form.addEventListener('submit', async (e) => {
    e.preventDefault()
    const fd = new FormData(form)
    statusCard.classList.remove('hidden')

    const res = await fetch('/start', {method:'POST', body: fd})
    const data = await res.json()
    const taskId = data.task_id
    link.href = `/results/${taskId}`

    const ev = new EventSource(`/events/${taskId}`)
    ev.onmessage = (m) => {
      if (m.data && m.data !== '.') {
        logEl.textContent += m.data + "\n"
        logEl.scrollTop = logEl.scrollHeight
      }
    }

    const t = setInterval(async () => {
      const p = await (await fetch(`/progress/${taskId}`)).json()
      if (p.total>0) {
        const pct = Math.round((p.completed / p.total) * 100)
        progressBar.style.width = pct + '%'
      }
      elapsedEl.textContent = fmtTime(p.elapsed||0)
      etaEl.textContent = p.status==='running' ? fmtTime(p.eta||0) : '00:00'

      if (p.status !== 'running'){
        clearInterval(t)
        ev.close()
        goto.classList.remove('hidden')
      }
    }, 900)
  })
})

// RESULTS: render e regeneração
async function renderResults(taskId, results){
  const wrap = document.querySelector('#results')
  wrap.innerHTML = ''
  const chunks = Object.keys(results).map(k => parseInt(k, 10)).sort((a,b)=>a-b)
  chunks.forEach(chunkIdx => {
    const arr = results[chunkIdx] || []
    arr.forEach((item, i) => {
      const card = document.createElement('div')
      card.className = 'result-card'
      card.innerHTML = `
        <img src="${item.url}" alt="Imagem ${i+1} do bloco ${chunkIdx+1}">
        <div class="meta">
          <small>Bloco ${chunkIdx+1} — Img ${i+1}</small>
          <button class="regen-btn" data-chunk="${chunkIdx}" data-index="${i}">Regenerar</button>
        </div>
      `
      wrap.appendChild(card)
    })
  })

  wrap.addEventListener('click', async (e) => {
    const btn = e.target.closest('.regen-btn')
    if (!btn) return
    const chunk = btn.dataset.chunk
    const idx = btn.dataset.index

    btn.disabled = true
    btn.textContent = 'Regenerando…'

    const fd = new FormData()
    fd.append('task_id', document.querySelector('#results').dataset.task)
    fd.append('chunk_idx', chunk)
    fd.append('img_index', idx)

    const res = await fetch('/regenerate', {method:'POST', body: fd})
    const data = await res.json()
    if (data.ok){
      const card = btn.closest('.result-card')
      const img = card.querySelector('img')
      img.src = data.url + `?t=${Date.now()}`
    }
    btn.disabled = false
    btn.textContent = 'Regenerar'
  })
}
