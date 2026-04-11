require('dotenv').config();
const Alpaca = require('@alpacahq/alpaca-trade-api');

const alpaca = new Alpaca({
  keyId: process.env.APCA_API_KEY_ID,
  secretKey: process.env.APCA_API_SECRET_KEY,
  baseUrl: 'https://paper-api.alpaca.markets',
  paper: true,
});

async function testConnection() {
  try {
    const account = await alpaca.getAccount();
    console.log('✅ Connected to Alpaca Paper Trading');
    console.log('-----------------------------------');
    console.log(`Account ID:     ${account.id}`);
    console.log(`Status:         ${account.status}`);
    console.log(`Buying Power:   $${parseFloat(account.buying_power).toFixed(2)}`);
    console.log(`Portfolio Value:$${parseFloat(account.portfolio_value).toFixed(2)}`);
    console.log(`Cash:           $${parseFloat(account.cash).toFixed(2)}`);
    console.log('-----------------------------------');
    return account;
  } catch (err) {
    console.error('❌ Connection failed:', err.message);
    throw err;
  }
}

testConnection();

module.exports = { alpaca };
