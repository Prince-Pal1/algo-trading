require('dotenv').config();
const { alpaca } = require('./connection');

// Place a market order
async function placeOrder({ symbol, qty, side, type = 'market', timeInForce = 'gtc' }) {
  try {
    const order = await alpaca.createOrder({
      symbol,
      qty,
      side,        // 'buy' or 'sell'
      type,        // 'market', 'limit', 'stop', 'stop_limit'
      time_in_force: timeInForce,
    });
    console.log(`✅ Order placed: ${side.toUpperCase()} ${qty} ${symbol}`);
    console.log(`   Order ID: ${order.id}`);
    console.log(`   Status:   ${order.status}`);
    return order;
  } catch (err) {
    console.error('❌ Order failed:', err.message);
    throw err;
  }
}

// Get all open positions
async function getPositions() {
  try {
    const positions = await alpaca.getPositions();
    if (positions.length === 0) {
      console.log('No open positions.');
      return [];
    }
    console.log('Open Positions:');
    positions.forEach(p => {
      console.log(`  ${p.symbol}: ${p.qty} shares @ $${parseFloat(p.avg_entry_price).toFixed(2)} | P&L: $${parseFloat(p.unrealized_pl).toFixed(2)}`);
    });
    return positions;
  } catch (err) {
    console.error('❌ Failed to get positions:', err.message);
    throw err;
  }
}

// Close a position
async function closePosition(symbol) {
  try {
    const result = await alpaca.closePosition(symbol);
    console.log(`✅ Closed position: ${symbol}`);
    return result;
  } catch (err) {
    console.error(`❌ Failed to close ${symbol}:`, err.message);
    throw err;
  }
}

// Get open orders
async function getOrders() {
  try {
    const orders = await alpaca.getOrders({ status: 'open' });
    console.log(`Open orders: ${orders.length}`);
    orders.forEach(o => {
      console.log(`  ${o.side.toUpperCase()} ${o.qty} ${o.symbol} — ${o.type} — ${o.status}`);
    });
    return orders;
  } catch (err) {
    console.error('❌ Failed to get orders:', err.message);
    throw err;
  }
}

module.exports = { placeOrder, getPositions, closePosition, getOrders };
