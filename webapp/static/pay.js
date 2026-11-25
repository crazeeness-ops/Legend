// Toss Payments browser SDK (v2) helpers. The SDK URL and client key come from our server;
// the secret key never reaches the browser.
const loaded = {};

function loadSdk(url) {
  if (!loaded[url]) {
    loaded[url] = new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = url;
      s.onload = () => (window.TossPayments ? resolve(window.TossPayments) : reject(new Error("결제 모듈을 불러오지 못했어요")));
      s.onerror = () => reject(new Error("결제 모듈을 불러오지 못했어요. 네트워크를 확인해 주세요"));
      document.head.append(s);
    });
  }
  return loaded[url];
}

async function paymentFor(info) {
  const TossPayments = await loadSdk(info.sdk_url);
  return TossPayments(info.client_key).payment({ customerKey: info.customer_key });
}

// one-time card payment → Toss redirects to success_url / fail_url
export async function requestCardPayment(checkout) {
  const payment = await paymentFor(checkout);
  await payment.requestPayment({
    method: "CARD",
    amount: { currency: "KRW", value: checkout.amount },
    orderId: checkout.order_id,
    orderName: checkout.order_name,
    successUrl: checkout.success_url,
    failUrl: checkout.fail_url,
    customerEmail: checkout.customer_email,
    customerName: checkout.customer_name,
  });
}

// card registration for subscriptions (자동결제) → success_url gets customerKey & authKey
export async function requestBillingAuth(info) {
  const payment = await paymentFor(info);
  await payment.requestBillingAuth({
    method: "CARD",
    successUrl: info.success_url,
    failUrl: info.fail_url,
    customerEmail: info.customer_email,
    customerName: info.customer_name,
  });
}
