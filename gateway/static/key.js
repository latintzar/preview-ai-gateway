/* The password field and CSRF token stay inside this dedicated gateway origin. */
let csrf;
async function bootstrap() {
  const response = await fetch('/api/session', {method:'POST', credentials:'include',
    headers:{'Content-Type':'application/json'}, body:'{}'});
  if (!response.ok) throw new Error('Open the HTTPS key window again.');
  csrf = (await response.json()).csrf;
}
const status = document.querySelector('#status');
const field = document.querySelector('#key');
document.querySelector('#key-form').addEventListener('submit', async event => {
  event.preventDefault(); status.textContent = 'Saving key…';
  try {
    await bootstrap();
    const response = await fetch('/api/key', {method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json','X-CSRF-Token':csrf}, body:JSON.stringify({key:field.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || 'Check the key and try again.');
    status.textContent = 'Key ready. Return to the preview.';
  } catch (error) { status.textContent = error.message; }
  finally { field.value = ''; }
});
document.querySelector('#forget').addEventListener('click', async () => {
  try {
    await bootstrap();
    const response = await fetch('/api/key', {method:'DELETE', credentials:'include', headers:{'X-CSRF-Token':csrf}});
    if (!response.ok) throw new Error('Open this key window again.');
    status.textContent = 'Key forgotten.';
  } catch (error) { status.textContent = error.message; }
  finally { field.value = ''; }
});
