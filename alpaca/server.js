const { Server } = require('@modelcontextprotocol/sdk/server/index.js');
const { StdioServerTransport } = require('@modelcontextprotocol/sdk/server/stdio.js');
const { CallToolRequestSchema, ListToolsRequestSchema } = require('@modelcontextprotocol/sdk/types.js');
const Alpaca = require('@alpacahq/alpaca-trade-api');

const alpaca = new Alpaca({
  keyId: process.env.ALPACA_API_KEY,
  secretKey: process.env.ALPACA_SECRET_KEY,
  baseUrl: process.env.ALPACA_BASE_URL || 'https://paper-api.alpaca.markets',
  paper: true,
});

const server = new Server(
  { name: 'alpaca', version: '1.0.0' },
  { capabilities: { tools: {} } }
);

// ── Tool definitions ──────────────────────────────────────────────────────────
server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: 'get_account',
      description: 'Get Alpaca account info: buying power, cash, portfolio value, status.',
      inputSchema: { type: 'object', properties: {} },
    },
    {
      name: 'place_order',
      description: 'Place a buy or sell order on Alpaca.',
      inputSchema: {
        type: 'object',
        required: ['symbol', 'qty', 'side'],
        properties: {
          symbol:      { type: 'string',  description: 'Ticker symbol, e.g. AAPL' },
          qty:         { type: 'number',  description: 'Number of shares (fractional ok)' },
          side:        { type: 'string',  enum: ['buy', 'sell'] },
          type:        { type: 'string',  enum: ['market', 'limit', 'stop', 'stop_limit'], default: 'market' },
          limit_price: { type: 'number',  description: 'Required for limit / stop_limit orders' },
          stop_price:  { type: 'number',  description: 'Required for stop / stop_limit orders' },
          time_in_force: { type: 'string', enum: ['day', 'gtc', 'ioc', 'fok'], default: 'gtc' },
        },
      },
    },
    {
      name: 'get_positions',
      description: 'List all open positions with P&L.',
      inputSchema: { type: 'object', properties: {} },
    },
    {
      name: 'close_position',
      description: 'Close (liquidate) an open position by symbol.',
      inputSchema: {
        type: 'object',
        required: ['symbol'],
        properties: {
          symbol: { type: 'string', description: 'Ticker to close, e.g. AAPL' },
        },
      },
    },
    {
      name: 'get_orders',
      description: 'List orders. Defaults to open orders.',
      inputSchema: {
        type: 'object',
        properties: {
          status: { type: 'string', enum: ['open', 'closed', 'all'], default: 'open' },
          limit:  { type: 'number', description: 'Max orders to return (default 20)' },
        },
      },
    },
    {
      name: 'cancel_order',
      description: 'Cancel an open order by order ID.',
      inputSchema: {
        type: 'object',
        required: ['order_id'],
        properties: {
          order_id: { type: 'string', description: 'Alpaca order UUID' },
        },
      },
    },
    {
      name: 'get_quote',
      description: 'Get the latest quote (bid/ask/last) for a symbol.',
      inputSchema: {
        type: 'object',
        required: ['symbol'],
        properties: {
          symbol: { type: 'string', description: 'Ticker symbol, e.g. AAPL' },
        },
      },
    },
  ],
}));

// ── Tool handlers ─────────────────────────────────────────────────────────────
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case 'get_account': {
        const a = await alpaca.getAccount();
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              id:              a.id,
              status:          a.status,
              cash:            parseFloat(a.cash),
              buying_power:    parseFloat(a.buying_power),
              portfolio_value: parseFloat(a.portfolio_value),
              equity:          parseFloat(a.equity),
              day_trade_count: a.daytrade_count,
              pattern_day_trader: a.pattern_day_trader,
            }, null, 2),
          }],
        };
      }

      case 'place_order': {
        const orderParams = {
          symbol:        args.symbol.toUpperCase(),
          qty:           args.qty,
          side:          args.side,
          type:          args.type || 'market',
          time_in_force: args.time_in_force || 'gtc',
        };
        if (args.limit_price) orderParams.limit_price = args.limit_price;
        if (args.stop_price)  orderParams.stop_price  = args.stop_price;

        const order = await alpaca.createOrder(orderParams);
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              id:         order.id,
              status:     order.status,
              symbol:     order.symbol,
              side:       order.side,
              qty:        order.qty,
              type:       order.order_type,
              filled_qty: order.filled_qty,
              created_at: order.created_at,
            }, null, 2),
          }],
        };
      }

      case 'get_positions': {
        const positions = await alpaca.getPositions();
        if (positions.length === 0) {
          return { content: [{ type: 'text', text: 'No open positions.' }] };
        }
        const result = positions.map(p => ({
          symbol:          p.symbol,
          qty:             parseFloat(p.qty),
          avg_entry_price: parseFloat(p.avg_entry_price),
          current_price:   parseFloat(p.current_price),
          market_value:    parseFloat(p.market_value),
          unrealized_pl:   parseFloat(p.unrealized_pl),
          unrealized_plpc: (parseFloat(p.unrealized_plpc) * 100).toFixed(2) + '%',
          side:            p.side,
        }));
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      case 'close_position': {
        const result = await alpaca.closePosition(args.symbol.toUpperCase());
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              id:     result.id,
              symbol: result.symbol,
              side:   result.side,
              qty:    result.qty,
              status: result.status,
            }, null, 2),
          }],
        };
      }

      case 'get_orders': {
        const orders = await alpaca.getOrders({
          status: args.status || 'open',
          limit:  args.limit  || 20,
        });
        if (orders.length === 0) {
          return { content: [{ type: 'text', text: 'No orders found.' }] };
        }
        const result = orders.map(o => ({
          id:         o.id,
          symbol:     o.symbol,
          side:       o.side,
          qty:        o.qty,
          filled_qty: o.filled_qty,
          type:       o.order_type,
          status:     o.status,
          created_at: o.created_at,
        }));
        return { content: [{ type: 'text', text: JSON.stringify(result, null, 2) }] };
      }

      case 'cancel_order': {
        await alpaca.cancelOrder(args.order_id);
        return { content: [{ type: 'text', text: `Order ${args.order_id} cancelled.` }] };
      }

      case 'get_quote': {
        const symbol = args.symbol.toUpperCase();
        const snap = await alpaca.getSnapshot(symbol);
        return {
          content: [{
            type: 'text',
            text: JSON.stringify({
              symbol,
              last:   snap.latestTrade?.p,
              bid:    snap.latestQuote?.bp,
              ask:    snap.latestQuote?.ap,
              volume: snap.dailyBar?.v,
              open:   snap.dailyBar?.o,
              high:   snap.dailyBar?.h,
              low:    snap.dailyBar?.l,
              close:  snap.dailyBar?.c,
            }, null, 2),
          }],
        };
      }

      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (err) {
    return {
      content: [{ type: 'text', text: `Error: ${err.message}` }],
      isError: true,
    };
  }
});

// ── Start ─────────────────────────────────────────────────────────────────────
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
