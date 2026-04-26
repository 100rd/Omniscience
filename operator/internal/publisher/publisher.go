// Package publisher emits operator events to NATS JetStream.
//
// The publisher is intentionally narrow: a single Publish method that takes
// a pre-built Event, serialises it to JSON, and publishes synchronously
// with ack confirmation. JetStream provides at-least-once and DLQ routing —
// see ADR-0007 §Ingestion.
package publisher

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/nats-io/nats.go"
	"github.com/nats-io/nats.go/jetstream"

	"github.com/100rd/omniscience/operator/internal/entity"
)

// Publisher is the operator's outbound interface to NATS JetStream.
type Publisher interface {
	Publish(ctx context.Context, ev *entity.Event) error
	Close() error
}

// natsPublisher is the production implementation backed by a real NATS
// connection. The connection and JetStream context are owned by the
// publisher and closed via Close().
type natsPublisher struct {
	conn          *nats.Conn
	js            jetstream.JetStream
	subjectPrefix string
}

// New connects to NATS at url, optionally authenticating via a credentials
// file path, and returns a Publisher. The connection is established
// synchronously; New returns an error if the broker is unreachable.
//
// credsFile may be empty in development. In every non-dev environment the
// Helm chart wires it to a Secret-backed file path — see ADR-0007 §ACL.
func New(url, credsFile, subjectPrefix string) (Publisher, error) {
	if url == "" {
		return nil, errors.New("publisher: nats url is required")
	}
	opts := []nats.Option{
		nats.Name("omniscience-operator"),
		nats.MaxReconnects(-1), // unbounded reconnect; the operator runs forever
	}
	if credsFile != "" {
		opts = append(opts, nats.UserCredentials(credsFile))
	}
	conn, err := nats.Connect(url, opts...)
	if err != nil {
		return nil, fmt.Errorf("publisher: nats connect: %w", err)
	}
	js, err := jetstream.New(conn)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("publisher: jetstream context: %w", err)
	}
	return &natsPublisher{
		conn:          conn,
		js:            js,
		subjectPrefix: subjectPrefix,
	}, nil
}

// Publish marshals the event to JSON and publishes synchronously to
// "{subjectPrefix}.{workspace_id}". Returns the publish error verbatim so
// the caller (controller) can NAK / requeue on failure.
func (p *natsPublisher) Publish(ctx context.Context, ev *entity.Event) error {
	if ev == nil {
		return errors.New("publisher: event must not be nil")
	}
	// Defence-in-depth: catch a missing workspace at the publisher boundary
	// even though entity.PodToEvent never produces one.
	if ev.WorkspaceID.String() == "00000000-0000-0000-0000-000000000000" {
		return errors.New("publisher: refusing to publish event with zero workspace_id")
	}
	body, err := json.Marshal(ev)
	if err != nil {
		return fmt.Errorf("publisher: marshal event: %w", err)
	}
	subject := p.subjectPrefix + "." + ev.WorkspaceID.String()
	if _, err := p.js.Publish(ctx, subject, body); err != nil {
		return fmt.Errorf("publisher: jetstream publish %s: %w", subject, err)
	}
	return nil
}

// Close drains the underlying NATS connection. Safe to call multiple times.
func (p *natsPublisher) Close() error {
	if p.conn != nil && !p.conn.IsClosed() {
		return p.conn.Drain()
	}
	return nil
}
