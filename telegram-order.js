(() => {
  const API_URL = 'https://vibeboost-2.onrender.com/api/order';

  const tg = window.Telegram?.WebApp;
  const form = document.getElementById('orderForm');
  const btn = document.getElementById('submitBtn');

  let sending = false;

  tg?.ready();
  tg?.expand();

  // Постоянное поле диагностики: можно прочитать и скопировать.
  const debug = document.createElement('pre');
  debug.style.cssText = `
    white-space:pre-wrap;overflow-wrap:anywhere;
    padding:12px;margin-top:12px;
    background:#0b1222;color:#eee;
    border:1px solid #526080;border-radius:10px;
    font:12px/1.5 monospace;
  `;
  form.append(debug);

  debug.textContent =
    `Версия: fix-3\nOrigin: ${location.origin}\nAPI: ${API_URL}`;

  const baseOpen = window.openOrderModal;

  window.openOrderModal = function(id) {
    baseOpen(id);
    btn.hidden = false;
    btn.textContent = 'Отправить заявку';
    document.getElementById('vb-telegram-panel')?.remove();

    const note = document.getElementById('specialTypeNote');
    if (note) {
      note.textContent = selected?.type !== 'Default'
        ? 'Дополнительные параметры уточнит администратор.'
        : '';
    }
  };

  async function submit(event) {
    event.preventDefault();
    if (sending || !selected) return;

    if (!tg?.initData) {
      debug.textContent =
        'Открой приложение кнопкой Mini App в Telegram-боте.';
      return;
    }

    const service = selected;
    const input = document.getElementById('orderQuantity');
    const quantityText = input.value.trim();
    const quantity = Number(quantityText);
    const link = document.getElementById('orderLink').value.trim();

    if (
      !/^\d+$/.test(quantityText) ||
      !Number.isSafeInteger(quantity) ||
      quantity < Number(service.min) ||
      quantity > Number(service.max)
    ) {
      debug.textContent =
        `Количество должно быть от ${service.min} до ${service.max}.`;
      return;
    }

    if (!link) {
      debug.textContent = 'Укажи ссылку или username.';
      return;
    }

    sending = true;
    btn.disabled = true;
    btn.textContent = 'Отправка…';

    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 90000);
    const started = Date.now();

    debug.textContent =
      `Версия: fix-3\nOrigin: ${location.origin}\n` +
      `POST ${API_URL}\nОжидаем ответ…`;

    try {
      const response = await fetch(API_URL, {
        method: 'POST',
        credentials: 'omit',
        headers: {'Content-Type': 'application/json'},
        signal: controller.signal,
        body: JSON.stringify({
          initData: tg.initData,
          order: {
            service: service.service,
            name: service.name,
            network: service.network,
            category: service.category,
            link,
            quantity,
            total: (Number(service.rate) * quantity / 1000).toFixed(2)
          }
        })
      });

      const text = await response.text();

      debug.textContent +=
        `\nHTTP ${response.status}\nОтвет: ${text.slice(0, 1500)}`;

      let result;
      try {
        result = JSON.parse(text);
      } catch {
        throw new Error('Сервер вернул не JSON. Смотри ответ выше.');
      }

      if (!response.ok || !result.ok) {
        throw new Error(result.error || `HTTP ${response.status}`);
      }

      debug.textContent += '\n✅ Заявка отправлена администратору.';
    } catch (error) {
      debug.textContent +=
        `\n❌ ${error.name}: ${error.message}` +
        `\nПрошло: ${Math.round((Date.now() - started) / 1000)} сек.`;

      if (error.name === 'TypeError') {
        debug.textContent +=
          '\nОтвет недоступен: проверяем CORS и логи Render.';
      }

      if (error.name === 'AbortError') {
        debug.textContent += '\nИстекло ожидание 90 секунд.';
      }

      debug.textContent +=
        '\nПеред повторной отправкой проверь чат админа: ' +
        'заказ мог дойти, даже если ответ потерялся.';
    } finally {
      clearTimeout(timer);
      sending = false;
      btn.disabled = false;
      btn.textContent = 'Отправить заявку';
    }
  }

  // Убираем старый inline-обработчик и назначаем один новый.
  form.removeAttribute('onsubmit');
  form.onsubmit = submit;
  window.submitOrder = submit;
})();
